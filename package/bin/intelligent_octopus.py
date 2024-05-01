import import_declare_test
import sys
import json

import datetime
from splunklib import modularinput as smi
from octopus_modinput import OctopusModInput
APPNAME="TA-octopusenergy"

class INTELLIGENT_OCTOPUS(OctopusModInput):

    def __init__(self):
        super(INTELLIGENT_OCTOPUS, self).__init__(APPNAME, "intelligent_octopus", False)

    def get_scheme(self):
        scheme = smi.Scheme('intelligent_octopus')
        scheme.description = 'Intelligent Octopus slots'
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

        return scheme

    def validate_input(self, definition):
        return

    def collect_data(self, inputs, ew):
        for input_name, input_item in inputs.inputs.items():
            self.load_account(account_name=input_item['account'])
            for type in ["planned","completed"]:
                self.log_warning(f"Getting {type}")
                r_json = self.get_dispatches(type=type)

                if r_json:
                    for reading in r_json:
                        self.log_warning(reading)
                        event = self.new_event(time=datetime.datetime.now(), host="localhost", index=input_item['index'], source=input_name, sourcetype=f"octopusenergy:io:{type}dispatch", data=json.dumps(reading))
                        ew.write_event(event)



if __name__ == '__main__':
    exit_code = INTELLIGENT_OCTOPUS().run(sys.argv)
    sys.exit(exit_code)