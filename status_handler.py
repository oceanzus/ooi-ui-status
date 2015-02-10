#!/usr/bin/env python

"""
@package status_handler
@file status_handler.py
@author Edna Donoughe (based on work of James Case)
@brief WSGI service supporting request for all routes in OOI UI Flask App, then process each static route for execution status
"""

from gevent.pywsgi import WSGIServer
from os.path import exists
import psycopg2
import psycopg2.extras
import simplejson as json
from simplejson.compat import StringIO
import yaml
import requests

KEY_SERVICE = "service"
ALIVE = "alive"
CHECK_CONNECTIONS = "checkconnections"
FETCH_STATS = "fetchstats"

CONTENT_TYPE_JSON = [('Content-type', 'text/json')]
CONTENT_TYPE_TEXT = [('Content-type', 'text/html')]
OK_200 = '200 OK'
BAD_REQUEST_400 = '400 Bad Request'

class StatusHandler(object):
    """
    WSGI service that fetches the routes for OOI UI Flask App (based on app.url_map).
    Execution time determined for each static route.
    Allows for switching between LIVE and DEMO mode in the settings file (future use)
    Provides for postgres connectivity check.
    Provides for uframe connectivity check. (future use)

    Sample requests:
        http://localhost:4070/service=alive
        http://localhost:4070/service=checkconnections
        http://localhost:4070/service=fetchstats
    """
    wsgi_url       = None
    wsgi_port      = None

    routes_url     = None
    routes_port    = None
    routes_command = None
    routes_timeout_connect = None
    routes_timeout_read    = None

    postgresql_host     = None
    postgresql_port     = None
    postgresql_username = None
    postgresql_password = None
    postgresql_database = None

    uframe_url  = None
    uframe_port = None
    uframe_username = None
    uframe_password = None

    service_mode = None
    debug = False

    def __init__(self):
        """
        :return:
        """
        # Open the settings.yml or settings_local.yml file
        settings = None
        try:
            if exists("status_settings.yml"):
                stream = open("status_settings.yml")
                settings = yaml.load(stream)
                stream.close()
            else:
                raise IOError('No settings.yml or settings_local.yml file exists!')

        except IOError, err:
            print 'IOError: %s' % err.message

        self.wsgi_url = settings['status_handler']['wsgi_server']['url']
        self.wsgi_port = settings['status_handler']['wsgi_server']['port']

        self.routes_url = settings['status_handler']['wsgi_server']['routes_url']
        self.routes_port = settings['status_handler']['wsgi_server']['routes_port']
        self.routes_command = settings['status_handler']['wsgi_server']['routes_command']
        self.routes_timeout_connect = settings['status_handler']['wsgi_server']['routes_timeout_connect']
        self.routes_timeout_read = settings['status_handler']['wsgi_server']['routes_timeout_read']

        self.postgresql_host = settings['status_handler']['postgresql_server']['host']
        self.postgresql_port = settings['status_handler']['postgresql_server']['port']
        self.postgresql_username = settings['status_handler']['postgresql_server']['username']
        self.postgresql_password = settings['status_handler']['postgresql_server']['password']
        self.postgresql_database = settings['status_handler']['postgresql_server']['database']

        self.uframe_url = settings['status_handler']['uframe_server']['url']
        self.uframe_port = settings['status_handler']['uframe_server']['port']
        self.uframe_username = settings['status_handler']['uframe_server']['username']
        self.uframe_password = settings['status_handler']['uframe_server']['password']

        self.service_mode = settings['status_handler']['service_mode']

        self.startup()

    def startup(self):
        """
        Start status handler WSGI service to determine route execution performance.
        list of route(s) to be processed is determined dynamically from result of route_command
        """
        try:
            WSGIServer((self.wsgi_url, self.wsgi_port), self.application).serve_forever()

        except IOError, err:
            print "The WSGI server at IP address " + self.wsgi_url + \
                  " failed to start on port " + str(self.wsgi_port) + "\nError message: " + str(err)

        except Exception, err:
            print "GENERAL EXCEPTION: The WSGI server at IP address " + self.wsgi_url + \
                  " failed to start on port " + str(self.wsgi_port) + "Error message: " + str(err)

    def application(self, env, start_response):

        request = env['PATH_INFO']
        request = request[1:]
        output = ''
        if self.debug: print '\n' + request + '\n'
        req = request.split("&")
        param_dict = {}
        if len(req) > 1:
            for param in req:
                params = param.split("=")
                param_dict[params[0]] = params[1]
        else:
            if "=" in request:
                params = request.split("=")
                param_dict[params[0]] = params[1]
            else:
                start_response(OK_200, CONTENT_TYPE_TEXT)
                return ['<b>' + request + '</br>' + output + '</b>']

        if KEY_SERVICE in param_dict:
            # Simply check if the service is responding (alive)
            # Returns: html
            if param_dict[KEY_SERVICE] == ALIVE:
                start_response(OK_200, CONTENT_TYPE_JSON)
                input_str={'Service Response': 'Alive'}
                return self.format_json(input_str)

            # Check the postgresql connections
            # Returns: html
            elif param_dict[KEY_SERVICE] == CHECK_CONNECTIONS:
                #TODO: Add UFRAME connection check
                postgresql_connected = self.check_postgresql_connection()
                if postgresql_connected:
                    start_response(OK_200, CONTENT_TYPE_JSON)
                    input_str={'Database': {'Connection': 'Alive'}}
                    return self.format_json(input_str)
                else:
                    start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                    input_str={'Database': {'Connection': 'Error'}}
                    return self.format_json(input_str)

            # Fetch all routes for OOI UI App; identify static routes; determine the execution time for all static routes.
            # Store result(s) in psql database; one record per route exercised.
            # Returns: JSON
            elif param_dict[KEY_SERVICE] == FETCH_STATS:

                # Check required configuration parameters are not empty
                if not self.routes_port or not self.routes_url or not self.routes_command:
                    start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                    input_str={'ERROR': 'routes_port, routes_url and routes_command must not be empty ; check config values'}
                    return self.format_json(input_str)

                # Get timestamp for this scenario (or group) of status checks for routes
                import datetime as dt
                scenario_timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Prepare url for fetching list of (route, endpoints) tuples
                actual_route_url = 'http://' + self.routes_url + ':' + str(self.routes_port) + '/' + self.routes_command

                # get list of (route, endpoint) tuples for routes with 'GET' method in rule of app.url_map
                # filter routes into two lists based on type of route: static or dynamic
                result = None
                try:
                    list_result = requests.get(actual_route_url,timeout=(self.routes_timeout_connect, self.routes_timeout_read))
                    if list_result:
                        if list_result.status_code == 200:
                            if list_result.json():
                                result = list_result.json()
                    else:
                        start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                        input_str={'ERROR': 'no content returned from ' + self.routes_command + '; check config routes_* values'}
                        return self.format_json(input_str)

                except Exception, err:
                    start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                    input_str={'ERROR': 'processing terminated due to exception while processing routes_command: ' + err}
                    return self.format_json(input_str)

                # for all 'static' routes, execute url and get elapsed time for request-response;
                # returns response result (as json) and status dictionary.
                # Each status dictionary contains: status_code, route_url, route_endpoint, timespan;
                # route_url and route_endpoint added
                # All status dictionaries gathered in statuses list
                if result:
                    routes = result['routes']
                    static_routes, dynamic_routes = self.separate_routes(routes)
                    statuses = []
                    try:
                        for static_tuple in static_routes:
                            sr = static_tuple[0]
                            se = static_tuple[1]
                            actual_route_url = 'http://' + self.routes_url + ':' + str(self.routes_port)  + sr
                            try:
                                status = self.url_get_status(actual_route_url)
                                status['status']['route_url'] = sr
                                status['status']['route_endpoint'] = se

                            except Exception, err:
                                print 'WARNING: exception while processing route: %s, error: %s' % (sr, err)
                                print 'WARNING: exception status: %s' % status

                            statuses.append(status)

                    except Exception, err:
                        start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                        input_str={'ERROR': 'exception while processing static route: ' + sr + 'error: ' + err}
                        return self.format_json(input_str)

                    # use contents of statuses and timestamp to write to db
                    psql_result = self.postgresql_write_stats(scenario_timestamp, statuses)
                    if psql_result:
                        start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                        input_str={'ERROR': 'error writing to Table \'performance_stats\' in database \'' + self.postgresql_database + '\''}
                        return self.format_json(input_str)

                    # response dictionary (final_results) contains 'timestamp' and 'stats'; stats is list of
                    # status results for all routes processed.
                    final_result = {}
                    final_result['timestamp'] = scenario_timestamp[:]      # timestamp (of this performance run/scenario)
                    final_result['stats'] = statuses[:]                    # list of status(es)

                    # prepare and return successful response
                    input_str = final_result
                    start_response(OK_200, CONTENT_TYPE_JSON)
                    return self.format_json(input_str)

            # Specified service is not valid
            # Returns: html
            else:
                start_response(BAD_REQUEST_400, CONTENT_TYPE_JSON)
                input_str={'ERROR': 'Specified service parameter is incorrect or unknown; ' + 'Request: ' + request }
                return self.format_json(input_str)

        start_response(OK_200, CONTENT_TYPE_TEXT)
        input_str='{Request: ' + request + '}{Response: ' + output + '}'
        return self.format_json(input_str)

    def format_json(self, input_str=None):
        """
        Formats input; returns JSON
        :param input_str:
        :return:
        """
        io = StringIO()
        json.dump(input_str, io)
        io.write('}')
        return io.getvalue()

    def get_postgres_connection(self):
        """
        :return:
        """
        try:
            conn = psycopg2.connect(
                database=self.postgresql_database,
                user=self.postgresql_username,
                password=self.postgresql_password,
                host=self.postgresql_host,
                port=self.postgresql_port)
            return conn
        except psycopg2.DatabaseError, err:
            print err
            return err

    def check_postgresql_connection(self):
        """
        :return:
        """
        conn = self.get_postgres_connection()
        print type(conn)
        if type(conn) == psycopg2.OperationalError:
            return None
        else:
            return conn.status

    def postgresql_write_stats(self, ts, stats):
        '''
        for each stat in stats, write status to database
        '''
        try:
            conn = self.get_postgres_connection()
            c = conn.cursor(cursor_factory = psycopg2.extras.RealDictCursor)
            for s in stats:
                r = s['status']
                #print 'r: %s ' % r
                query = 'insert into performance_stats(timestamp, status_code, url_processed, timespan, route_url, route_endpoint) ' + \
                    'values(\'%s\', \'%s\', \'%s\', %s, \'%s\', \'%s\' );' % \
                    (ts, r['status_code'], r['url_processed'], str(r['timespan']), r['route_url'], r['route_endpoint'] )
                c.execute(query)
                conn.commit()

            return None

        except Exception, err:
            print err
        except psycopg2.DatabaseError, err:
            print err
            return err
        finally:
            if conn:
                conn.close()

    def separate_routes(self, routes):
        '''
        for list of (route, endpoint) tuples, separate into static and dynamic route lists
        '''
        dynamic_routes = []
        static_routes = []
        for res in routes:
            route = res[0]
            endpoint = res[1]
            if "<" in route:
                dynamic_routes.append((route, endpoint))
            else:
                if route not in static_routes:
                    static_routes.append((route, endpoint))

        return static_routes, dynamic_routes

    def url_get_status(self, query_string):
        '''
        process query string, determine execution time (in seconds), return status dictionary containing:
        status_code, [actual] url_processed, timespan (execution time for request-response in seconds)
        (Note: does not return result of query, just status)
        '''
        result_str={'status': {'timespan': '', 'status_code': '', 'url_processed': ''}}
        try:
            if not query_string:
                raise Exception('ERROR: url_get_status query_string parameter is empty')

            import datetime as dt
            a = dt.datetime.now()   # start time
            result = requests.get(query_string,timeout=(self.routes_timeout_connect, self.routes_timeout_read))
            b = dt.datetime.now()   # end time
            d = b-a                 # delta
            timespan       = d.total_seconds()
            status_code    = result.status_code
            url_processed  = query_string
            result_str['status']['timespan']      = timespan
            result_str['status']['status_code']   = status_code
            result_str['status']['url_processed'] = url_processed
            return result_str

        except Exception, err:
            print 'error: exception processing url get_status; err: %s' % err
            result_str = {}

        return result_str

if __name__ == "__main__":
    StatusHandler()