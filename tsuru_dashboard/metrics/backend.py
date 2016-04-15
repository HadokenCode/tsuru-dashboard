from tsuru_dashboard import settings

import requests
import json
import datetime
import re


class MetricNotEnabled(Exception):
    pass


def get_backend(app, token, date_range=None, process_name=None):
    headers = {'authorization': token}
    url = "{}/apps/{}/metric/envs".format(settings.TSURU_HOST, app["name"])
    response = requests.get(url, headers=headers)

    es_query = ElasticSearchFilter(app=app["name"], process_name=process_name, date_range=date_range).query()

    if response.status_code == 200:
        data = response.json()
        if "METRICS_ELASTICSEARCH_HOST" in data:
            return ElasticSearch(
                url=data["METRICS_ELASTICSEARCH_HOST"],
                query=es_query,
                date_range=date_range
            )

    if "envs" in app and "ELASTICSEARCH_HOST" in app["envs"]:
        return ElasticSearch(
            url=app["envs"]["ELASTICSEARCH_HOST"],
            query=es_query,
            date_range=date_range
        )

    raise MetricNotEnabled


NET_AGGREGATION = {
    "units": {
        "terms": {
            "field": "host"
        },
        "aggs": {
            "delta": {
                "scripted_metric": {
                    "init_script": """
_agg['max'] = [ts: 0, val: null]
_agg['min'] = [ts: 0, val: null]
""",
                    "map_script": """
ts = doc['@timestamp'][0]
val = doc['value'][0]
if (ts > _agg.max.ts) {
    _agg.max.ts = ts
    _agg.max.val = val
}
if (_agg.min.ts == 0 || ts < _agg.min.ts) {
    _agg.min.ts = ts
    _agg.min.val = val
}
""",
                    "reduce_script": """
max = null
min = null
for (a in _aggs) {
    if (max == null || a.max.ts > max.ts) {
        max = a.max
    }
    if (min == null || a.min.ts < min.ts) {
        min = a.min
    }
}
dt = max.ts - min.ts
if (dt > 0) {
    return ((max.val - min.val)/1024)/(dt/1000)
} else {
    return 0
}
"""
                }
            }
        }
    }
}


class ElasticSearchFilter(object):
    def __init__(self, app, process_name=None, date_range=None):
        self.date_range = date_range
        if app is not None:
            self.filter = self.app_filter(app, process_name)

    def query(self):
        return {
            "filtered": {
                "filter": self.filter
            }
        }

    def app_filter(self, app, process_name):
        f = {
            "bool": {
                "must": [
                    {
                        "bool": {
                            "should": [
                                self.term_filter("app", app),
                                self.term_filter("app.raw", app)
                            ]
                        }
                    },
                    self.timestamp_filter(self.date_range)
                ],
            }
        }

        if process_name:
            p = {
                "bool": {
                    "should": [
                        self.term_filter("process", process_name),
                        self.term_filter("process.raw", process_name)
                    ]
                }
            }
            f["bool"]["must"].append(p)
        return f

    def term_filter(self, field, value):
        return {"term": {field: value}}

    def timestamp_filter(self, date_range):
        if date_range is None:
            date_range = "1h"
        return {"range": {"@timestamp": {"gte": "now-" + date_range, "lt": "now"}}}


class ElasticSearch(object):
    def __init__(self, url, query, date_range="1h"):
        if date_range == "1h":
            self.index = ".measure-tsuru-{}".format(datetime.datetime.utcnow().strftime("%Y.%m.%d"))
        else:
            self.index = ".measure-tsuru-{}.*".format(datetime.datetime.utcnow().strftime("%Y"))
        self.url = url
        self.filtered_query = query
        self.date_range = date_range

    def post(self, data, metric):
        url = "{}/{}/{}/_search".format(self.url, self.index, metric)
        result = requests.post(url, data=json.dumps(data))
        return result.json()

    def process(self, data, formatter=None):
        if not formatter:
            def default_formatter(x):
                return x
            formatter = default_formatter

        def processor(result, bucket):
            bucket_max = formatter(bucket["stats"]["max"])
            bucket_min = formatter(bucket["stats"]["min"])
            bucket_avg = formatter(bucket["stats"]["avg"])
            if not result:
                result = {
                    "max": [],
                    "min": [],
                    "avg": [],
                }
            result["max"].append([bucket["key"], bucket_max])
            result["min"].append([bucket["key"], bucket_min])
            result["avg"].append([bucket["key"], bucket_avg])
            return result, bucket_min, bucket_max

        return self.base_process(data, processor)

    def cpu_max(self, interval=None):
        query = self.query(interval=interval)
        response = self.post(query, "cpu_max")
        process = self.process(response)
        return process

    def mem_max(self, interval=None):
        query = self.query(interval=interval)
        return self.process(self.post(query, "mem_max"), formatter=lambda x: x / (1024 * 1024))

    def swap(self, interval=None):
        query = self.query(interval=interval)
        return self.process(self.post(query, "swap"), formatter=lambda x: x / (1024 * 1024))

    def netrx(self, interval=None):
        return self.net_metric("netrx", interval)

    def nettx(self, interval=None):
        return self.net_metric("nettx", interval)

    def net_metric(self, kind, interval=None):
        query = self.query(interval=interval, aggregation=NET_AGGREGATION)
        return self.base_process(self.post(query, kind), self.net_process)

    def net_process(self, result, bucket):
        value = 0
        for in_bucket in bucket["units"]["buckets"]:
            value += in_bucket["delta"]["value"]
        if not result:
            result["requests"] = []
        result["requests"].append([bucket["key"], value])
        return result, value, value

    def units(self, interval=None):
        aggregation = {"units": {"cardinality": {"field": "host"}}}
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "cpu_max"), self.units_process)

    def units_process(self, result, bucket):
        value = bucket["units"]["value"]
        if not result:
            result["units"] = []
        result["units"].append([bucket["key"], value])
        return result, value, value

    def requests_min(self, interval=None):
        aggregation = {"sum": {"sum": {"field": "count"}}}
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "response_time"), self.requests_min_process)

    def requests_min_process(self, result, bucket):
        value = bucket["sum"]["value"]
        if not result:
            result["requests"] = []
        result["requests"].append([bucket["key"], value])
        return result, value, value

    def response_time(self, interval=None):
        aggregation = {
            "stats": {"stats": {"field": "value"}},
            "percentiles": {"percentiles": {"field": "value"}}
        }
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "response_time"), self.response_time_process)

    def response_time_process(self, result, bucket):
        bucket_max = bucket["stats"]["max"]
        bucket_min = bucket["stats"]["min"]
        bucket_avg = bucket["stats"]["avg"]
        if not result:
            result = {
                "max": [],
                "min": [],
                "avg": [],
                "p95": [],
                "p99": [],
            }
        result["max"].append([bucket["key"], bucket_max])
        result["min"].append([bucket["key"], bucket_min])
        result["avg"].append([bucket["key"], bucket_avg])
        result["p95"].append([bucket["key"], bucket["percentiles"]["values"]["95.0"]])
        result["p99"].append([bucket["key"], bucket["percentiles"]["values"]["99.0"]])
        return result, bucket_min, bucket_max

    def top_slow(self, interval=None):
        aggregation = {
            "top": {
                "terms": {
                    "script": "doc['method'].value +'U'+doc['path.raw'].value +'U'+doc['status_code'].value"
                },
                "aggs": {"max": {"max": {"field": "value"}}}
            }
        }
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "response_time"), self.top_slow_process)

    def top_slow_process(self, result, bucket):
        max_response = 0
        for b in bucket["top"]["buckets"]:
            response_time = b["max"]["value"]
            if response_time > max_response:
                max_response = response_time
            r = r'(?P<method>\w+)U(?P<path>.*)U(?P<status_code>\d{3})'
            info = re.search(r, b["key"])
            path = info.groupdict()['path']
            status_code = info.groupdict()['status_code']
            method = info.groupdict()['method']

            if path not in result:
                result[path] = []

            result[path].append([bucket["key"], response_time, status_code, method])

        return result, 0, max_response

    def http_methods(self, interval=None):
        aggregation = {"method": {"terms": {"field": "method"}}}
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "response_time"), self.http_methods_process)

    def http_methods_process(self, result, bucket):
        max_value = 0
        min_value = 0

        for doc in bucket["method"]["buckets"]:
            value = doc["doc_count"]
            method = doc["key"]

            if method not in result:
                result[method] = []

            if value < min_value:
                min_value = value

            if value > max_value:
                max_value = value

            result[method].append([bucket["key"], value])
        return result, min_value, max_value

    def status_code(self, interval=None):
        aggregation = {"status_code": {"terms": {"field": "status_code"}}}
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "response_time"), self.status_code_process)

    def status_code_process(self, result, bucket):
        max_value = 0
        min_value = 0

        for doc in bucket["status_code"]["buckets"]:
            value = doc["doc_count"]
            code = doc["key"]

            if code not in result:
                result[code] = []

            if value < min_value:
                min_value = value

            if value > max_value:
                max_value = value

            result[code].append([bucket["key"], value])
        return result, min_value, max_value

    def connections(self, interval=None):
        aggregation = {
            "connection": {
                "terms": {"field": "connection.raw"}
            }
        }
        query = self.query(interval=interval, aggregation=aggregation)
        return self.base_process(self.post(query, "connection"), self.connections_process)

    def connections_process(self, result, bucket):
        min_value = 0
        max_value = 0
        for doc in bucket["connection"]["buckets"]:
            size = doc["doc_count"]
            conn = doc["key"]
            if conn not in result:
                result[conn] = []

            if size < min_value:
                min_value = size

            if size > max_value:
                max_value = size

            result[conn].append([bucket["key"], size])
        return result, min_value, max_value

    def base_process(self, data, processor):
        result = {}
        min_value = None
        max_value = 0

        if "aggregations" in data and len(data["aggregations"]["date"]["buckets"]) > 0:
            for bucket in data["aggregations"]["date"]["buckets"]:
                result, min_v, max_v = processor(result, bucket)
                if min_value is None or min_v < min_value:
                    min_value = min_v
                if max_v > max_value:
                    max_value = max_v

        return {
            "data": result,
            "min": min_value or 0,
            "max": max_value + 1,
        }

    def query(self, interval=None, aggregation=None):
        if not interval:
            interval = "1m"

        if not aggregation:
            aggregation = {"stats": {"stats": {"field": "value"}}}
        return {
            "query": self.filtered_query,
            "size": 0,
            "aggs": {
                "date": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "interval": interval
                    },
                    "aggs": aggregation,
                }
            }
        }
