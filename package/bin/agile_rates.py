import import_declare_test
import sys
import json
import datetime

from splunklib import modularinput as smi
from splunktaucclib.modinput_wrapper.base_modinput import BaseModInput
from splunktaucclib.splunk_aoblib.setup_util import Setup_Util

class AGILE_RATES(BaseModInput):

    def __init__(self):
        super(AGILE_RATES, self).__init__("TA-octopusenergy", "agile_rates", False)

    def get_scheme(self):
        scheme = smi.Scheme('agile_rates')
        scheme.description = '30-min Agile rates'
        scheme.use_external_validation = True
        scheme.streaming_mode_xml = True
        scheme.use_single_instance = False

        scheme.add_argument(
            smi.Argument(
                'name',
                title='Name',
                description='Name',
                required_on_create=True
            )
        )

        scheme.add_argument(
            smi.Argument(
                'account',
                required_on_create=True,
            )
        )

        scheme.add_argument(
            smi.Argument(
                'example_help_link',
                required_on_create=False,
            )
        )

        scheme.add_argument(
            smi.Argument(
                'rate_code',
                required_on_create=True,
            )
        )

        scheme.add_argument(
            smi.Argument(
                'start_date',
                required_on_create=False,
            )
        )

        return scheme

    def validate_input(self, definition):
        return

    def round_minutes(self, dt, direction, resolution):
        new_minute = (dt.minute // resolution + (1 if direction == 'up' else 0)) * resolution
        return dt + datetime.timedelta(minutes=new_minute - dt.minute)

    def stream_events(self, inputs, ew):
        # input_items = [{'count': len(inputs.inputs)}]
        # for input_name, input_item in inputs.inputs.items():
        #     input_item['name'] = input_name
        #     input_items.append(input_item)
        # event = smi.Event(
        #     data=json.dumps(input_items),
        #     sourcetype='agile_rates',
        # )
        # ew.write_event(event)
        uri = inputs.metadata["server_uri"]
        session_key = inputs.metadata['session_key']
        self.setup_util = Setup_Util(uri, session_key, self.logger)
        helper = self
        for input_name, input_item in inputs.inputs.items():

            checkpoint_key = "start_time"
            start_time = helper.get_check_point(checkpoint_key)
            if not start_time:
                start_time = input_item['start_date']
            self.log_warning(f"start_time={start_time}")
            proxy_settings = helper.get_proxy()
            rate_code = input_item['rate_code']
            if '.' not in start_time:
                start_time = start_time + '.0'
            start_dt = datetime.datetime.strptime(start_time,"%Y-%m-%d %H:%M:%S.%f")
            helper.log_info(start_dt)
            now = self.round_minutes(datetime.datetime.now(),'down',30)
            dt_diff = now - start_dt
            end_dt = start_dt + datetime.timedelta(days=1)
            if end_dt < start_dt:
                helper.log_warning("Not running as start time is later than end time")
                exit(0)
            new_rate_announce_time = datetime.datetime.combine(datetime.date.today(), datetime.time(16, 0))
            if end_dt < now:
                helper.log_info(f"Getting data for {start_dt} to {end_dt}")
            elif now > new_rate_announce_time:
                midnight_tonight = datetime.datetime.combine(datetime.date.today(), datetime.time(16, 0))
                tomorrow_cutoff = datetime.datetime.combine(datetime.date.today()+datetime.timedelta(days=1), datetime.time(23, 30))
                if start_dt > tomorrow_cutoff:
                    helper.log_warning("Start time is after 11pm tomorrow - no info available yet")
                    #            start_dt = datetime.datetime.combine(datetime.date.today(), datetime.time(23, 0))
                    #            end_dt = start_dt + datetime.timedelta(days=1)
                    exit(2)
                if start_dt >= tomorrow_cutoff:
                    helper.log_warning("No point looking for more than the cut off")
                    exit(3)
                if start_dt < tomorrow_cutoff:
                    helper.log_warning("Getting tomorrows data")
                    start_dt = self.round_minutes(datetime.datetime.now(),'down',30)
                    end_dt = start_dt + datetime.timedelta(days=1)

            if end_dt-start_dt == 0:
                helper.log_warning("Nothing to do!")
                exit(0)
            url="https://api.octopus.energy/v1/products/{}/electricity-tariffs/E-1R-{}-M/standard-unit-rates/?period_from={}&period_to={}".format(rate_code,rate_code,start_dt.isoformat(), end_dt.isoformat())
            helper.log_debug(f"Getting url={url}")
            method = "GET"
            response = helper.send_http_request(url, method, parameters=None, payload=None,
                                                headers=None, cookies=None, verify=True, cert=None,
                                                timeout=30, use_proxy=False)
            response.raise_for_status()
            r_json = response.json()
            helper.log_debug(response.content)

            for rate in r_json['results']:
                event = helper.new_event(time=rate['valid_from'], host="localhost", index=input_item['index'], source=input_name, sourcetype="octopusenergy:agile_rates", data=json.dumps(rate))
                ew.write_event(event)
            if len(r_json['results'])>0:
                helper.save_check_point(checkpoint_key, str(end_dt))


if __name__ == '__main__':
    exit_code = AGILE_RATES().run(sys.argv)
    sys.exit(exit_code)