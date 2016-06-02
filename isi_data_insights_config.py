"""
This file contains utility functions for configuring the IsiDataInsightsDaemon
via command line args and config file.
"""
import argparse
import ConfigParser
import getpass
import logging
import os
import isi_sdk
import sys
import urllib3

from isi_data_insights_daemon import StatsConfig, ClusterConfig
from isi_stats_client import IsiStatsClient


LOG = logging.getLogger(__name__)

DEFAULT_PID_FILE = "/var/run/isi_data_insights_d.pid"
DEFAULT_LOG_FILE = "/var/log/isi_data_insights_d.log"
DEFAULT_LOG_LEVEL = "INFO"
# name of the section in the config file where the main/global settings for the
# daemon are stored.
MAIN_CFG_SEC = "isi_data_insights_d"
# the number of seconds to wait between updates for stats that are
# continually kept up-to-date.
ONE_SEC = 1 # seconds
# the default minimum update interval (even if a particular stat key is updated
# at a higher rate than this we will still only query at this rate in order to
# prevent the cluster from being overloaded with stat queries).
MIN_UPDATE_INTERVAL = 30 # seconds
# name of the config file param that can be used to specify a lower
# MIN_UPDATE_INTERVAL.
MIN_UPDATE_INTERVAL_OVERRIDE_PARAM = "min_update_interval_override"

# keep track of auth data that we have username and passwords for so that we
# don't prompt more than once.
g_cluster_auth_data = {}
# keep track of the name and version of each cluster
g_cluster_names_and_versions = {}


def _verify_cluster_auth_data(cluster_address):
    # HACK: verify auth credentials by doing a query for "cluster.health"
    # stats key.
    try:
        _query_stats_metadata(cluster_address, ["cluster.health"])
    except isi_sdk.rest.ApiException as exc:
        print >> sys.stderr, "Invalid auth credentials for cluster: %s.\n"\
                "ERROR:\n%s." % (cluster_address, str(exc))
        sys.exit(1)


def _add_cluster_auth_data(cluster_address, username, password, verify_ssl):
    # update cluster auth data
    g_cluster_auth_data[cluster_address] = (username, password, verify_ssl)
    # if params are known then verify username and password
    if username is not None \
            and password is not None \
            and verify_ssl is not None:
        _verify_cluster_auth_data(cluster_address)


def _process_config_file_clusters(clusters):
    cluster_list = []
    cluster_configs = clusters.split(" ")
    for cluster_config in cluster_configs:
        # expected [username:password@]address[:bool]
        at_split = cluster_config.split("@")
        if len(at_split) == 2:
            user_pass_split = at_split[0].split(":", 1)
            if len(user_pass_split) != 2:
                print >> sys.stderr, "Config file contains invalid cluster "\
                        "config: %s in %s (expected <username>:<password> "\
                        "prefix)." % (cluster_config, clusters)
                sys.exit(1)
            username = user_pass_split[0]
            password = user_pass_split[1]
            # If they provide a username and password then verify_ssl defaults
            # to false. Otherwise, unless they explicity provide it in the
            # config, we will prompt them for that parameter when we prompt for
            # the username and password.
            verify_ssl = False
        else:
            username = None
            password = None
            verify_ssl = None
        verify_ssl_split = at_split[-1].split(":", 1)
        if len(verify_ssl_split) == 1:
            cluster_address = verify_ssl_split[0]
        else:
            try:
                # try to convert to a bool
                verify_ssl = eval(verify_ssl_split[-1])
                if type(verify_ssl) != bool:
                    raise Exception
            except Exception:
                print >> sys.stderr, "Config file contains invalid cluster "\
                        "config: %s in %s (expected True or False on end, "\
                        "but got %s)." % (cluster_config, clusters,
                                cluster_config[verify_ssl_index+1:])
                sys.exit(1)
            cluster_address = verify_ssl_split[0]
        # add to cache of known cluster auth usernames and passwords
        _add_cluster_auth_data(cluster_address, username, password, verify_ssl)
        cluster_list.append(cluster_address)

    return cluster_list


def _get_cluster_auth_data(cluster):
    try:
        username = password = verify_ssl = None
        # check if we already know the username and password
        username, password, verify_ssl = g_cluster_auth_data[cluster]
        if username is None or password is None or verify_ssl is None:
            # this happens when some of the auth params were provided in the
            # config file or cli, but not all.
            raise KeyError
    except KeyError:
        # get username and password for input clusters
        if username is None:
            username = raw_input("Please provide the username used to access "\
                    + cluster + " via PAPI: ")
        if password is None:
            password = getpass.getpass("Password: ")
        while verify_ssl is None:
            verify_ssl_resp = raw_input("Verify SSL cert [y/n]: ")
            if verify_ssl_resp == "yes" or verify_ssl_resp == "y":
                verify_ssl = True
            elif verify_ssl_resp == "no" or verify_ssl_resp == "n":
                verify_ssl = False
        # add to cache of known cluster auth usernames and passwords
        _add_cluster_auth_data(cluster, username, password, verify_ssl)

    return username, password, verify_ssl

def _build_api_client(cluster_address, username, password, verify_ssl):
    isi_sdk.configuration.username = username
    isi_sdk.configuration.password = password
    isi_sdk.configuration.verify_ssl = verify_ssl
    if verify_ssl is False:
        urllib3.disable_warnings()
    url = "https://" + cluster_address + ":8080"
    return isi_sdk.ApiClient(url)

def _query_cluster_name(cluster_address, username, password, verify_ssl):
    api_client = \
            _build_api_client(cluster_address, username, password, verify_ssl)
    # get the Cluster API
    cluster_api = isi_sdk.ClusterApi(api_client)
    try:
        resp = cluster_api.get_cluster_identity()
        return resp.name
    except isi_sdk.rest.ApiException:
        # if get_cluster_identity() doesn't work just use the address
        return cluster_address


def _query_cluster_version(cluster_address, username, password, verify_ssl):
    api_client = \
            _build_api_client(cluster_address, username, password, verify_ssl)
    url = "https://" + cluster_address + ":8080"
    # get the Cluster API
    cluster_api = isi_sdk.ClusterApi(api_client)
    version = 8.0
    # figure out which python sdk is installed - get_cluster_version was
    # added for 8.0, so only works on 8.0 clusters, so try to use that if
    # it is available, but if the cluster is not 8.0 then
    # get_cluster_version will throw an exception, which then we'll have to
    # assume it is a 7.2 cluster.
    if hasattr(cluster_api, "get_cluster_version"):
        node_versions = None
        try:
            version_resp = cluster_api.get_cluster_version()
            node_versions = version_resp.nodes
            # if any nodes are less than 8.0 then use that
            for node in version_resp.nodes:
                if node.release.startswith('v'):
                    node_version = float(node.release[1:4])
                    if node_version < version:
                        version = node_version
                        break
        except isi_sdk.rest.ApiException as exc:
            LOG.warning("Unable to determine version for cluster %s. " \
                    "Exception: %s." % (cluster_address, str(exc)))
            version = 7.2
    else:
        # if the 7.2 sdk is installed then even if the cluster is an 8.0
        # cluster it still must be treated like a 7.2 cluster.
        version = 7.2

    return version


def _build_cluster_configs(cluster_list):
    cluster_configs = []
    for cluster in cluster_list:
        username, password, verify_ssl = _get_cluster_auth_data(cluster)

        if cluster in g_cluster_names_and_versions:
            cluster_name, version = g_cluster_names_and_versions[cluster]
        else:
            cluster_name = \
                    _query_cluster_name(
                            cluster, username, password, verify_ssl)
            version = _query_cluster_version(
                            cluster, username, password, verify_ssl)
            g_cluster_names_and_versions[cluster] = cluster_name, version

        cluster_config = \
                ClusterConfig(cluster, username, password, version,
                        cluster_name, verify_ssl)
        cluster_configs.append(cluster_config)

    return cluster_configs


def _configure_stat_group(daemon,
        update_interval, cluster_list, stats_list):
    """
    Configure the daemon with some StatsConfigs.
    """
    cluster_configs = _build_cluster_configs(cluster_list)
    # configure daemon with stats
    if update_interval < MIN_UPDATE_INTERVAL:
        LOG.warning("The following stats are set to be queried at a faster "\
                "rate, %d seconds, than the MIN_UPDATE_INTERVAL of %d "\
                "seconds. To configure a shorter MIN_UPDATE_INTERVAL specify "\
                "it with the %s param in the %s section of the config file. "\
                "Stats:\n\t%s", update_interval, MIN_UPDATE_INTERVAL,
                    MIN_UPDATE_INTERVAL_OVERRIDE_PARAM, MAIN_CFG_SEC,
                    str(stats_list))
        update_interval = MIN_UPDATE_INTERVAL
    stats_config = \
        StatsConfig(cluster_configs, stats_list, update_interval)
    daemon.add_stats(stats_config)


def _query_stats_metadata(cluster, stat_names):
    """
    Query the specified cluster for the metadata of the stats specified in
    stat_names list.
    """
    username, password, verify_ssl = _get_cluster_auth_data(cluster)
    isi_stats_client = \
            IsiStatsClient(cluster, username, password, verify_ssl)
    return isi_stats_client.get_stats_metadata(stat_names)


def _compute_stat_group_update_intervals(update_interval_multiplier,
        cluster_list, stat_names, update_intervals):
    # update interval is supposed to be set relative to the collection
    # interval, which might be different for each stat and each cluster.
    for cluster in cluster_list:
        stats_metadata = _query_stats_metadata(cluster, stat_names)
        for stat_metadata in stats_metadata:
            # cache time is the length of time the system will store the
            # value before it updates.
            cache_time = -1
            if stat_metadata.default_cache_time:
                cache_time = \
                        ((stat_metadata.default_cache_time + 1)
                        # add one to the default_cache_time because the new
                        # value is not set until 1 second after the cache time.
                                * update_interval_multiplier)
            # the policy intervals seem to override the default cache time
            if stat_metadata.policies:
                smallest_interval = cache_time
                for policy in stat_metadata.policies:
                    if smallest_interval == -1:
                        smallest_interval = policy.interval
                    else:
                        smallest_interval = \
                            min(policy.interval,
                                    smallest_interval)
                cache_time = \
                        (smallest_interval * update_interval_multiplier)
            # if the cache_time is still -1 then it means that the statistic is
            # continually updated, so the fastest it can be queried is
            # once every second.
            if cache_time == -1:
                cache_time = ONE_SEC * update_interval_multiplier
            try:
                update_interval = update_intervals[cache_time]
                update_interval[0].add(cluster)
                update_interval[1].add(stat_metadata.key)
            except KeyError:
                # insert a new interval time
                update_intervals[cache_time] = \
                        (set([cluster]), set([stat_metadata.key]))


def _configure_stat_groups_via_file(daemon,
        config_file, stat_group, global_cluster_list):
    cluster_list = []
    cluster_list.extend(global_cluster_list)
    try:
        # process clusters specific to this stat group (if any)
        clusters_param = config_file.get(stat_group, "clusters")
        stat_group_clusters = _process_config_file_clusters(clusters_param)
        cluster_list.extend(stat_group_clusters)
        # remove duplicates
        cluster_list = list(set(cluster_list))
    except ConfigParser.NoOptionError:
        pass

    if len(cluster_list) == 0:
        print >> sys.stderr, "The %s stat group has no clusters to query."\
                % (stat_group)
        sys.exit(1)

    update_interval_param = config_file.get(stat_group, "update_interval")
    stat_names = config_file.get(stat_group, "stats").split()
    # remove duplicates
    stat_names = list(set(stat_names))

    update_intervals = {}
    if update_interval_param.startswith("*"):
        try:
            update_interval_multiplier = 1 if update_interval_param == "*" \
                    else int(update_interval_param[1:])
        except ValueError as exc:
            print >> sys.stderr, "Failed to parse update interval from %s "\
                    "stat group.\nERROR: %s" % (stat_group, str(exc))
            sys.exit(1)
        _compute_stat_group_update_intervals(
                update_interval_multiplier, cluster_list, stat_names,
                update_intervals)
    else:
        try:
            update_interval = int(update_interval_param)
        except ValueError as exc:
            print >> sys.stderr, "Failed to parse update interval from %s "\
                    "stat group.\nERROR: %s" % (stat_group, str(exc))
            sys.exit(1)
        update_intervals[update_interval] = \
                (cluster_list, stat_names)

    for update_interval, clusters_stats_tuple in update_intervals.iteritems():
        # first item in clusters_stats_tuple is the unique list of clusters
        # associated with the current update_interval, the second item is the
        # unique list of stats to query on the set of clusters at the current
        # update_interval.
        _configure_stat_group(daemon,
                update_interval,
                clusters_stats_tuple[0],
                clusters_stats_tuple[1])


def _configure_stat_groups_via_cli(daemon, args):
    if not args.update_intervals:
        # for some reason if i try to use default=[MIN_UPDATE_INTERVAL] in the
        # argparser for the update_intervals arg then my list always has a
        # MIN_UPDATE_INTERVAL in addition to any intervals actually provided by
        # the user on the command line, so i need to setup the default here
        args.update_intervals.append(MIN_UPDATE_INTERVAL)

    if len(args.stat_groups) != len(args.update_intervals):
        print >> sys.stderr, "The number of update intervals must be the "\
                + "same as the number of stat groups."
        sys.exit(1)

    cluster_list = args.clusters.split(",")
    # if args.clusters is the empty string then 1st element will be empty
    if cluster_list[0] == "":
        print >> sys.stderr, "Please provide at least one input cluster."
        sys.exit(1)

    # remove duplicates
    cluster_list = list(set(cluster_list))

    for index in range(0, len(args.stat_groups)):
        stats_list = args.stat_groups[index].split(",")
        # split always results in at least one item, so check if the first
        # item is empty to validate the stats input arg
        if stats_list[0] == "":
            print >> sys.stderr, "Please provide at least one stat name."
            sys.exit(1)
        update_interval = args.update_intervals[index]
        _configure_stat_group(daemon,
                update_interval, cluster_list, stats_list)


def _configure_stats_processor(daemon, stats_processor, processor_args):
    try:
        processor = __import__(stats_processor, fromlist=[''])
    except ImportError:
        print >> sys.stderr, "Unable to load stats processor: %s." \
                % stats_processor
        sys.exit(1)

    try:
        arg_list = processor_args.split(" ") \
                if processor_args != "" else []
        daemon.set_stats_processor(processor, arg_list)
    except AttributeError as exception:
        print >> sys.stderr, "Failed to configure %s as stats processor. %s" \
                % (stats_processor, str(exception))
        sys.exit(1)


def _log_level_str_to_enum(log_level):
    if log_level.upper() == "DEBUG":
        return logging.DEBUG
    elif log_level.upper() == "INFO":
        return logging.INFO
    elif log_level.upper() == "WARNING":
        return logging.WARNING
    elif log_level.upper() == "ERROR":
        return logging.ERROR
    elif log_level.upper() == "CRITICAL":
        return logging.CRITICAL
    else:
        print "Invalid logging level: " + log_level + ", setting to INFO."
        return logging.INFO


def _update_args_with_config_file(config_file, args):
    # command line args override config file params
    if args.pid_file is None \
            and config_file.has_option(MAIN_CFG_SEC, "pid_file"):
        args.pid_file = config_file.get(MAIN_CFG_SEC, "pid_file")
    if args.log_file is None \
            and config_file.has_option(MAIN_CFG_SEC, "log_file"):
        args.log_file = config_file.get(MAIN_CFG_SEC, "log_file")
    if args.log_level is None \
            and config_file.has_option(MAIN_CFG_SEC, "log_level"):
        args.log_level = config_file.get(MAIN_CFG_SEC, "log_level")


def _print_stat_groups(daemon):
    """
    Print out the list of stat sets that were configured for the daemon prior
    to starting it so that user can verify that it was configured as expected.
    """
    for update_interval, stat_set in daemon.get_next_stat_set():
        msg = "Configured stat set:\n\tClusters: %s\n\t"\
                "Update Interval: %d\n\tStat Keys: %s" \
                % (str(stat_set.cluster_configs), update_interval,
                        str(stat_set.stats))
        # print it to stdout and the log file.
        print msg
        LOG.debug(msg)


def configure_via_file(daemon, args, config_file):
    """
    Configure the daemon's stat groups and the stats processor via command line
    arguments and configuration file. The command line args override settings
    provided in the config file.
    """
    # Command line args override config file params
    if not args.stats_processor \
            and config_file.has_option(MAIN_CFG_SEC, "stats_processor") is True:
        args.stats_processor = config_file.get(MAIN_CFG_SEC, "stats_processor")
    if not args.processor_args \
            and config_file.has_option(
                    MAIN_CFG_SEC, "stats_processor_args") is True:
        args.processor_args = \
                config_file.get(MAIN_CFG_SEC, "stats_processor_args")
    _configure_stats_processor(daemon,
            args.stats_processor, args.processor_args)

    # check if the MAIN_CFG_SEC has the MIN_UPDATE_INTERVAL_OVERRIDE_PARAM
    if config_file.has_option(MAIN_CFG_SEC,
            MIN_UPDATE_INTERVAL_OVERRIDE_PARAM):
        global MIN_UPDATE_INTERVAL
        try:
            override_update_interval = int(
                    config_file.get(MAIN_CFG_SEC,
                        MIN_UPDATE_INTERVAL_OVERRIDE_PARAM))
        except ValueError as exc:
            print >> sys.stderr, "Failed to parse %s from %s "\
                    "section.\nERROR: %s" % (
                            MIN_UPDATE_INTERVAL_OVERRIDE_PARAM,
                            MAIN_CFG_SEC, str(exc))
            sys.exit(1)

        LOG.warning("Overriding MIN_UPDATE_INTERVAL of %d seconds with "\
                "%d seconds.", MIN_UPDATE_INTERVAL, override_update_interval)
        MIN_UPDATE_INTERVAL = override_update_interval

    # if there are any clusters, stats, or update_intervals specified via CLI
    # then try to configure the daemon using them first.
    if args.update_intervals or args.stat_groups or args.clusters:
        _configure_stat_groups_via_cli(daemon, args)
    global_cluster_list = []
    if args.clusters:
        global_cluster_list = args.clusters.split(",")
    elif config_file.has_option(MAIN_CFG_SEC, "clusters"):
        global_cluster_list = \
                _process_config_file_clusters(config_file.get(
                    MAIN_CFG_SEC, "clusters"))
    # remove duplicates
    global_cluster_list = list(set(global_cluster_list))

    # now configure with config file params too
    if config_file.has_option(MAIN_CFG_SEC, "active_stat_groups"):
        active_stat_groups = config_file.get(MAIN_CFG_SEC,
                "active_stat_groups").split()
        for stat_group in active_stat_groups:
            _configure_stat_groups_via_file(daemon,
                    config_file, stat_group, global_cluster_list)

    # check that at least one stat group was added to the daemon.
    if daemon.get_stat_set_count() == 0:
        print >> sys.stderr, "Please provide stat groups to query via "\
                "command line args or via config file parameters."
        sys.exit(1)

    _print_stat_groups(daemon)


def configure_via_cli(daemon, args):
    """
    Configure the daemon's stat groups and the stats processor via command line
    arguments.
    """
    _configure_stat_groups_via_cli(daemon, args)
    _configure_stats_processor(daemon,
            args.stats_processor, args.processor_args)

    _print_stat_groups(daemon)


def configure_logging_via_cli(args):
    """
    Setup the logging from command line args.
    """
    if args.action != "debug":
        if args.log_file is None:
            args.log_file = DEFAULT_LOG_FILE

        parent_dir = os.path.dirname(args.log_file)
        if parent_dir \
                and os.path.exists(parent_dir) is False:
            print >> sys.stderr, "Invalid log file path: %s." \
                    % (args.log_file)
            sys.exit(1)

        if args.log_level is None:
            args.log_level = DEFAULT_LOG_LEVEL

        log_level = _log_level_str_to_enum(args.log_level)
        logging.basicConfig(filename=args.log_file, level=log_level,
                format='%(asctime)s:%(name)s:%(levelname)s: %(message)s')
    else: # configure logging to stdout for 'debug' action
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG,
                format='%(asctime)s:%(name)s:%(levelname)s: %(message)s')


def configure_args_via_file(args):
    """
    Load the config_file, if there is one, then check if the pid_file,
    log_file, and log_level parameters are provided in the config file. If they
    are and they are not set via CLI args then use the config file to set them.
    """
    config_file = None
    if args.config_file is not None:
        try:
            config_file = ConfigParser.ConfigParser()
            with open(args.config_file, "r") as cfg_fp:
                config_file.readfp(cfg_fp)
        except Exception as exc:
            print >> sys.stderr, "Failed to parse config file: %s.\n"\
                    "ERROR:\n%s." % (args.config_file, str(exc))
            sys.exit(1)
        _update_args_with_config_file(config_file, args)
    return config_file


def process_pid_file_arg(pid_file):
    """
    Make sure the pid_file argument is a valid path. Set it to the default if
    it was not specified.
    """
    if pid_file is None:
        pid_file = DEFAULT_PID_FILE

    parent_dir = os.path.dirname(pid_file)
    if parent_dir \
            and os.path.exists(parent_dir) is False:
        print >> sys.stderr, "Invalid pid file path: %s." \
                % (pid_file)
        sys.exit(1)

    return os.path.abspath(pid_file)


def parse_cli():
    """
    Setup the command line args and parse them.
    """
    argparser = argparse.ArgumentParser(
            description='Starts, stops, or restarts the '\
                    'isi_data_insights_daemon.')
    argparser.add_argument('action', help="Specifies to 'start', 'stop', "
            "'restart', or 'debug' the daemon.")
    argparser.add_argument('-c', '--config-file', dest='config_file',
            help="Set the path to the config file.",
            action='store', default=None)
    argparser.add_argument('-a', '--processor-args', dest='processor_args',
            help="Specifies the args to pass to the start function of the "
            "results processor's start function.",
            action="store", default="")
    argparser.add_argument('-l', '--log-file', dest='log_file',
            help="Set the path to the log file.",
            action='store', default=None)
    argparser.add_argument('-e', '--log-level', dest='log_level',
            help="Set the logging level (debug, info, warning, error, or "
            "critical).", action='store', default=None)
    argparser.add_argument('-p', '--pid-file', dest='pid_file',
            help="Set the path to the daemon pid file.",
            action='store', default=None)
    argparser.add_argument('-x', '--stats-processor', dest='stats_processor',
            help="Name of the Python module used to process stats query "
            "results. The specified Python module must define "
            "a function named process(results_list) where results_list is a"
            "list of isi_sdk.models.statistics_current_stat objects."
            "StatisticsCurrentStat objects.  The module may also optionally "
            "define start(args) and stop() functions. Use the "
            "--processor-args to specify args to pass to the results "
            "processor's start function.",
            action='store', default=None)
    argparser.add_argument('-i', '--input-clusters', dest='clusters',
            help="Comma delimitted list of clusters to monitor (either "
            "hostnames or ip-addresses)",
            action='store', default="")
    argparser.add_argument('-s', '--stats', dest='stat_groups',
            help="Comma delimitted list of stat names to monitor. Accepts"
            "multiple.", default=[], action='append')
    argparser.add_argument('-u', '--update-interval', dest='update_intervals',
            help="Specifies how often, in seconds, the input clusters should "
            "be polled for each stat group. Accepts multiple.",
            action='append', default=[], type=int)

    return argparser.parse_args()
