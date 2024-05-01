import import_declare_test
import sys
import json
import os

import datetime
from splunklib import modularinput as smi
from octopus_modinput import OctopusModInput

APPNAME="TA-octopusenergy"

class LIVE_READINGS(OctopusModInput):

    def __init__(self):
        super(LIVE_READINGS, self).__init__(APPNAME, "live_readings", False)

    def get_scheme(self):
        scheme = smi.Scheme('live_readings')
        scheme.description = 'Live (OctoMini)'
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
            try:
                self.set_meter(input_item['meter'])
                self.load_account(account_name=input_item['account'])
                # mpan = self.meter_mpan
                # serial = self.meter_serial
                # mac = self.meter_mac
                meter_type = self.meter_type
                checkpoint_key = f"{input_name}_start_time"
                start_time = self.get_check_point(checkpoint_key)
                if not start_time:
                    start_dt = datetime.datetime.now() - datetime.timedelta(hours=48)  #input_item['start_date']
                    self.log_info(f"setting start_time={start_time}")
                else:
                    start_dt = datetime.datetime.strptime(start_time,"%Y-%m-%dT%H:%M:%S%z") + datetime.timedelta(seconds=10) #.replace(tzinfo=None)

                now = datetime.datetime.now(start_dt.tzinfo)
                end_dt = start_dt + datetime.timedelta(hours=3)

                if end_dt < start_dt:
                    self.log_info("Not running as start time is later than end time")
                    exit(0)

                self.log_info(f"Setting start_dt={start_dt}")
                if end_dt > now:
                    end_dt = now
    
                if end_dt-start_dt == 0:
                    self.log_info("Nothing to do!")
                    exit(0)

                r_json = self.get_live_usage(start_date=start_dt, end_date=end_dt)
                last_reading_time = False
                if r_json:
                    for reading in r_json:
                        reading['consumption'] = int(float(reading['consumption']))
                            # if 'costDelta' in reading:
                            #     del(reading['costDelta'])
                        event = self.new_event(time=reading['readAt'], host="localhost", index=input_item['index'], source=input_name, sourcetype=f"octopusenergy:live_meter_reading:{meter_type}", data=json.dumps(reading))
                        ew.write_event(event)
                        last_reading_time = reading['readAt']
                    if not last_reading_time:
                        last_reading_time = end_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
                    self.log_info(f"Setting checkpoint to {last_reading_time}")
                    self.save_check_point(checkpoint_key, str(last_reading_time))
            except(Exception) as e:
                self.log_warning(e)

if __name__ == '__main__':
    exit_code = LIVE_READINGS().run(sys.argv)
    sys.exit(exit_code)
