"""Inspect job flows to predict optimal pool of reserved instances that
minimizes the cost.

This module will take job flows from wherever you specify, and use them to
approximate the amount of reserved instances you should buy and how much money
you will save by doing so.

If you are looking for instructions to run the program, look at the
readme in the root EMRio folder.
"""
import json
import locale
import logging
from optparse import OptionParser

import boto
import pytz

from ec2_cost import EC2Info
from ec2_cost import instance_types_in_pool
from graph_jobs import Grapher
from job_handler import get_job_flows
from job_handler import load_job_flows_from_amazon
from optimizer import convert_to_yearly_estimated_hours
from optimizer import Optimizer
from simulate_jobs import Simulator


def main():
    option_parser = make_option_parser()
    options, args = option_parser.parse_args()
    timezone = pytz.timezone(options.timezone)
    EC2 = EC2Info(options.instance_costs)

    if options.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if options.dump:
        logging.info("Dumping job flow history into %s", options.dump)
        write_job_flow_history(options.dump)
        return

    job_flows = get_job_flows(options, timezone)
    logging.info('Finding optimal instance pool (this may take a minute or '
        'two)...')
    pool = get_best_instance_pool(job_flows,
                                options.optimized_file,
                                options.save,
                                EC2)
    optimal_logged_hours, demand_logged_hours = simulate_job_flows(job_flows,
                                                                    pool,
                                                                    EC2)
    output_statistics(optimal_logged_hours, pool, demand_logged_hours, EC2)

    Grapher(job_flows, pool, EC2).show(total_usage=options.total_usage,
                                        instance_usage=options.instance_usage)


def make_option_parser():
    description = 'Print a giant report on EMR usage.'
    option_parser = OptionParser(description=description)
    option_parser.add_option(
        '-v', '--verbose', dest='verbose', default=False, action='store_true',
        help='print more messages to stderr')
    option_parser.add_option(
        '-q', '--quiet', dest='quiet', default=False, action='store_true',
        help="Don't log status messages; just print the report.")
    option_parser.add_option(
        '-c', '--conf-path', dest='conf_path', default=None,
        help='Path to alternate mrjob.conf file to read from')
    option_parser.add_option(
        '--no-conf', dest='conf_path', action='store_false',
        help="Don't load mrjob.conf even if it's available")
    option_parser.add_option(
        '--max-days-ago', dest='max_days_ago', type='float', default=None,
        help=('Max number of days ago to look at jobs. By default, we go back'
            'as far as EMR supports (currently about 2 months)'))
    option_parser.add_option(
        '--max-day', dest='max_days', type='string', default=None,
        help=('The first day to last looking in the job flows. If any job'
            'ends after this day, it is discarded (e.g.: '
            '--max-days 2012/05/07)'))
    option_parser.add_option(
        '--min-day', dest='min_days', type='string', default=None,
        help=('The first day to start looking in the job flows. If any job'
            'starts before this day, it is discarded (e.g.: '
            '--max-days 2012/05/07)'))
    option_parser.add_option(
        '--file', dest='file_inputs', type='string', default=None,
        help="Input a file that has job flows JSON encoded. The format is 1 "
        "job per line or comma separated jobs.")
    option_parser.add_option(
        '-o', '--optimized', dest='optimized_file', type='string',
        default=None, help=("Uses a previously saved optimized pool instead of"
        " calculating it from the job flows"))
    option_parser.add_option(
        '--cache', dest='save', type='string', default=None,
        help='Save the optimized results so you dont calculate them multiple'
        ' times')
    option_parser.add_option(
        '-g', '--graph_instance_usage', dest='instance_usage',
        action='store_true', default=False, help='Load a graph'
        ' of the job flow history instance usage')
    option_parser.add_option(
        '--total_usage', dest='total_usage', action='store_true',
        default=False, help='Load a graph of total hourly usage for each'
        ' instance type')
    option_parser.add_option(
        '-d', '--dump-jobs', dest='dump', type='string', default=None,
        help="dumps a job history into the file specified. Won't run the"
        " optimizer.")
    option_parser.add_option(
        '-t', '--timezone', dest='timezone', type='string',
        default="US/Alaska", help="This option specifies a different timezone"
        " for the grapher to display in. The default timezone is 'US/Pacific'"
        " but to change it, use pytz string names")
    option_parser.add_option(
        '--instance-cost', dest='instance_costs', type='string',
        default="instance_costs/west_coast_1.yaml", help="This option"
        "specifies the cost zone you want to calculate for. The default is"
        " west-coast-1")
    return option_parser


def get_best_instance_pool(job_flows, optimized_filename, save_filename, EC2):
    """Returns the best instance flow based on the job_flows passed in or
    a file passed in by the user.

    Args:
        optimized_filename: the name of a file with the optimal instances in it
            if this is None, then get the data from Amazon.

        save_filename: the name of a file to save the calculated results to.
            If none, don't save the file.

        job_flows: A list of jobs flow dictionary objects.

    Returns:
        pool of best optimal instances.
    """
    if optimized_filename:
        pool = read_optimal_instances(optimized_filename)
    else:

        owned_reserved_instances = get_owned_reserved_instances(EC2)
        pool = Optimizer(job_flows, EC2).run(
                pre_existing_pool=owned_reserved_instances)

    if save_filename:
        write_optimal_instances(save_filename, pool)
    return pool


def write_optimal_instances(filename, pool):
    """Save optimal pool results.

    Format for saving is json.
    An example saved instance file is in the tests folder.
    Args:
        filename: name of the file to save the pool to.

        pool: A dict of reserved instances to buy.
    """
    with open(filename, 'w') as f:
        json.dump(pool, f)


def read_optimal_instances(filename):
    """Reads the file name provided and uses it to create an optimal instance
    pool.

    If you want to see the format of these files, check the tests folder.

    Returns:
        pool: The utilization class and instances read from the file specified.
    """
    with open(filename, 'r') as f:
        pool = json.load(f)
        return pool


def write_job_flow_history(filename):
    """This will write out all the job flows to a file.

    Args:
        filename: file to write or append job json objects to.

    """
    job_flows = load_job_flows_from_amazon(None, None)
    json_ready_job_flows = {}

    # Job flow dicts have a lot of boto objects that need to be removed first.
    # This only keeps relevant info to write to the file.
    for job in job_flows:
        json_job = {}
        json_job['startdatetime'] = job.get('startdatetime', None)
        json_job['enddatetime'] = job.get('enddatetime', None)
        json_job['jobflowid'] = job['jobflowid']
        json_job['instancegroups'] = []
        for instance in job['instancegroups']:
            json_instance = {}
            json_instance['instancetype'] = instance['instancetype']
            json_instance['instancerequestcount'] = (
                instance['instancerequestcount'])
            json_job['instancegroups'].append(json_instance)
        json_ready_job_flows[json_job['jobflowid']] = json_job

    # Error will be thrown if there is no file, so we catch and continue.
    try:
        with open(filename, 'r+') as f:
            for line in f:
                json_job = json.loads(line)
                json_ready_job_flows[json_job['jobflowid']] = json_job
    except IOError:
        pass

    with open(filename, 'w') as f:
        for json_job in json_ready_job_flows.values():
            f.write(str(json.JSONEncoder().encode(json_job)) + '\n')


def simulate_job_flows(job_flows, pool, EC2):
    """Simulates the job flows using the pool, and will also simulate pure
    on-demand hours with no pool and return both.

    Returns:
        optimal_logged_hours: The amount of hours that each reserved instance
            used from the given job flow.

        demand_logged_hours: The amount of hours used per instance on just
            purely on demand instances, no reserved instances. Use this as a
            control group.
    """
    job_flows_begin_time = min(job.get('startdatetime') for job in job_flows)
    job_flows_end_time = max(job.get('enddatetime') for job in job_flows)
    interval_job_flows = job_flows_end_time - job_flows_begin_time

    EMPTY_INSTANCE_POOL = EC2.init_empty_reserve_pool()
    optimal_simulator = Simulator(job_flows, pool, EC2)
    demand_simulator = Simulator(job_flows, EMPTY_INSTANCE_POOL, EC2)
    optimal_logged_hours = optimal_simulator.run()
    demand_logged_hours = demand_simulator.run()

    convert_to_yearly_estimated_hours(demand_logged_hours, interval_job_flows)
    convert_to_yearly_estimated_hours(optimal_logged_hours, interval_job_flows)
    return optimal_logged_hours, demand_logged_hours


def get_owned_reserved_instances(EC2):
    """Pulls the currently owned reserved instances from Amazon AWS

    Returns:
        purchased_reserved_instances: A dict of instances you currently own.
        looks like:
        instances = {
            UTILIZATION_CLASS: {
                INSTANCE_NAME: OWNED_AMOUNT
            }
        }
    """
    boto_logger = logging.getLogger('boto')
    boto_logger.disabled = True
    ec2_conn = boto.connect_ec2()
    boto_reserved_instances = ec2_conn.get_all_reserved_instances()
    ec2_conn.close()
    purchased_reserved_instances = EC2.init_empty_reserve_pool()
    for reserved_instance in boto_reserved_instances:
        utilization_class = reserved_instance.offering_type
        instance_type = reserved_instance.instance_type
        purchased_reserved_instances[utilization_class][instance_type] += (
            reserved_instance.instance_count)
    return purchased_reserved_instances


def calculate_instances_to_buy(purchased_instances, optimal_pool, EC2):
    """Calculate the amount of instances to buy from amazon.

    Takes the difference of purchased instances from optimal pool or -1 if
    too many instances are already owned than the optimal amount.

    Args:
        purchased_instances: The amount of instances currently owned.

        optimal_pool: The calculated amount of instances that minimizes the
            cost based on the job flows.
    Returns:
        reserved_instances_to_buy: A dict of instances to buy. Structured like
            pool.
    """
    calculated_difference = lambda y, x: (x - y) if x - y >= 0 else -1
    reserved_instances_to_buy = EC2.init_empty_reserve_pool()

    for utilization_class in optimal_pool:
        for instance_type in optimal_pool[utilization_class]:
            reserved_instances_to_buy[utilization_class][instance_type] = (
                calculated_difference(
                    purchased_instances[utilization_class][instance_type],
                    optimal_pool[utilization_class][instance_type]))
    return reserved_instances_to_buy


def intWithCommas(x):
    if type(x) not in [type(0), type(0L)]:
        raise TypeError("Parameter must be an integer.")
    if x < 0:
        return '-' + intWithCommas(-x)
    result = ''
    while x >= 1000:
        x, r = divmod(x, 1000)
        result = ",%03d%s" % (r, result)
    return "%d%s" % (x, result)


def output_statistics(log, pool, demand_log, EC2):
    """Once everything is calculated, output here"""
    EMPTY_INSTANCE_POOL = EC2.init_empty_reserve_pool()
    optimized_cost, optimized_upfront_cost = EC2.calculate_cost(log, pool)
    demand_cost, _ = EC2.calculate_cost(demand_log, EMPTY_INSTANCE_POOL)

    owned_reserved_instances = get_owned_reserved_instances(EC2)
    buy_instances = calculate_instances_to_buy(owned_reserved_instances, pool,
        EC2)

    all_instances = instance_types_in_pool(pool)
    all_instances.union(instance_types_in_pool(owned_reserved_instances))

    print "%20s %15s %15s %15s" % ('', 'Optimal', 'Owned', 'To Purchase')
    for utilization_class in EC2.RESERVE_PRIORITIES:
        print "%-20s" % (utilization_class)
        for machine in all_instances:
            print "%20s %15d %15d %15d" % (machine,
                pool[utilization_class][machine],
                owned_reserved_instances[utilization_class][machine],
                buy_instances[utilization_class][machine])

    print
    print " Hours Used By Instance type **************"
    for utilization_class in demand_log:
        for machine in demand_log[utilization_class]:
            print "\t%s: %s" % (machine,
                    intWithCommas(int(demand_log[utilization_class][machine])))

    optimized_cost_fmt = intWithCommas(int(optimized_cost))
    optimized_upfront_cost_fmt = intWithCommas(int(optimized_upfront_cost))
    demand_cost_fmt = intWithCommas(int(demand_cost))
    difference_cost = intWithCommas(int(demand_cost - optimized_cost))
    print
    print "Cost difference:"
    print "Hourly cost for all instance: $%s" % optimized_cost_fmt
    print "Upfront Cost for all instances: $%s" % optimized_upfront_cost_fmt
    print "Cost for all On-Demand: $%s" % demand_cost_fmt
    print "Money Saved: $%s" % difference_cost

if __name__ == '__main__':
    main()
