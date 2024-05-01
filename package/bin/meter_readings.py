import import_declare_test
import sys
import json

import datetime
from splunklib import modularinput as smi
from splunktaucclib.modinput_wrapper.base_modinput import BaseModInput
from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
from solnlib import conf_manager
from base64 import b64encode
from octopus_modinput import OctopusModInput
APPNAME="TA-octopusenergy"

class METER_READINGS(OctopusModInput):

    def __init__(self):
        super(METER_READINGS, self).__init__(APPNAME, "meter_readings", False)

    def get_scheme(self):
        scheme = smi.Scheme('meter_readings')
        scheme.description = 'Meter Readings / Usage'
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
                'meter',
                required_on_create=True,
            )
        )
        scheme.add_argument(
            smi.Argument(
                'example_help_link',
                required_on_create=False,
            )
        )

        return scheme

    def round_minutes(self, dt, direction, resolution):
        new_minute = (dt.minute // resolution + (1 if direction == 'up' else 0)) * resolution
        return dt + datetime.timedelta(minutes=new_minute - dt.minute)

    def validate_input(self, definition):
        return

    def collect_data(self, inputs, ew):
        proxy_settings = self.get_proxy()

        for input_name, input_item in inputs.inputs.items():

            self.set_meter(input_item['meter'])
            mpan = self.meter_mpan
            serial = self.meter_serial
            mac = self.meter_mac
            meter_type = self.meter_type
            checkpoint_key = f"{input_name}_start_time"
            start_time = self.get_check_point(checkpoint_key)

            if not start_time:
                start_time = "2020-01-01T00:00:00Z" #input_item['start_date']
            self.log_warning(f"start_time={start_time}")
            if '.' not in start_time:
                start_time = start_time + '.0'
            start_dt = datetime.datetime.strptime(start_time,"%Y-%m-%dT%H:%M:%S%z.%f").replace(tzinfo=None)
            now = self.round_minutes(datetime.datetime.now(),'down',30)
            dt_diff = now - start_dt
            end_dt = start_dt + datetime.timedelta(days=365)
            if end_dt < start_dt:
                self.log_warning("Not running as start time is later than end time")
                exit(0)

            self.log_info(f"Getting data for {start_dt} to {end_dt}")

            if end_dt-start_dt == 0:
                self.log_warning("Nothing to do!")
                exit(0)
            if meter_type=="Gas":
                meter_type_url_segment = "gas-meter-points"
            elif meter_type=="Electric":
                meter_type_url_segment = "electricity-meter-points"
            else:
                self.log_critical(f"Unknown meter_type={meter_type}")
                meter_type_url_segment = ""
                exit(1)

            url=f"https://api.octopus.energy/v1/{meter_type_url_segment}/{mpan}/meters/{serial}/consumption/?period_from={start_dt.isoformat()}&period_to={end_dt.isoformat()}&page_size=25000"
            self.log_warning(f"Getting url={url}")
            method = "GET"
            self.load_account(input_item['account'])
            headers = { "Authorization" : self.account_config['access_token']}
            response = self.send_http_request(url, method, parameters=None, payload=None,
                                                headers=headers, cookies=None, verify=True, cert=None,
                                                timeout=None, use_proxy=True)
            response.raise_for_status()
            r_json = response.json()

            for reading in r_json['results']:
                event = self.new_event(time=reading['interval_start'], host="localhost", index=input_item['index'], source=input_name, sourcetype=f"octopusenergy:meter_reading:{meter_type}", data=json.dumps(reading))
                ew.write_event(event)
            if len(r_json['results'])>0:
                last_reading_time = r_json['results'][0]['interval_end']
                self.save_check_point(checkpoint_key, str(last_reading_time))


if __name__ == '__main__':
    exit_code = METER_READINGS().run(sys.argv)
    sys.exit(exit_code)