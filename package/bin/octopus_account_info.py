import import_declare_test
import splunk.admin as admin
import splunk.clilib.cli_common as scc
import logging

from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
from octopus_modinput import OctopusModInput

logger = logging.getLogger()

import splunktaucclib.common.log as stulog
APPNAME = "TA-octopusenergy"
FIELDS = ["account"]

class OctopusAccountInfo(admin.MConfigHandler):

    def setup(self):
        stulog.logger.error("SETUP")
        for arg in FIELDS:
            self.supportedArgs.addOptArg(arg)


    @staticmethod
    def validate_params(must_params, opt_params, **params):
        pass

    def handleList(self, confInfo):
        if not self.callerArgs or not self.callerArgs.get("account"):
            logger.error("Missing OctopusEnergy credentials")
            raise Exception("Missing OctopusEnergy credentials")

        octopus_helper = OctopusModInput(app_namespace=APPNAME, input_name="AccountInfo")
        octopus_helper.session_key = self.getSessionKey()
        octopus_helper.splunk_uri = scc.getMgmtUri()
        octopus_helper.setup_util = Setup_Util(octopus_helper.splunk_uri, octopus_helper.session_key)

        octopus_helper.load_account(account_name=self.callerArgs["account"][0])
        meters = octopus_helper.get_kracken_meters()
        for meterType in meters:
            for meter in meters[meterType]:
                confInfo[f"{meterType}#{meter['mpan']}#{meter['serial']}#{meter['mac']}"]["metername"] = f"{meterType} - {meter['serial']}"
        return


if __name__ == "__main__":
    admin.init(OctopusAccountInfo, admin.CONTEXT_APP_AND_USER)