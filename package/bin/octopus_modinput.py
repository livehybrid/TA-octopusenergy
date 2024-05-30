import import_declare_test
from splunktaucclib.modinput_wrapper.base_modinput import BaseModInput
from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
import splunk.Intersplunk as si
from solnlib import conf_manager
from splunklib import client as client
import json
import jwt
import time
import os

class OctopusModInput(BaseModInput):
    appName = "TA-octopusenergy"
    restPath = "ta_octopusenergy"

    @property
    def app(self):
        return self.appName

    def __init__(self, app_namespace, input_name, use_single_instance=False):
        super(OctopusModInput, self).__init__(app_namespace, input_name, False)

    def get_kracken_meters(self):
        account_info = self.get_kracken_account_info()
        meters = {
            "Gas":[],
            "Electric":[]
        }
        for kraken_electric_mpan in account_info['account']['properties'][0]['electricityMeterPoints']:
            electric_mpan = kraken_electric_mpan['mpan']
            for kraken_electric_meter in kraken_electric_mpan['meters']:
                meters["Electric"].append({
                    "mpan":electric_mpan,
                    "serial":kraken_electric_meter['serialNumber'],
                    "mac":kraken_electric_meter["smartDevices"][0]["deviceId"]
                })

        for kraken_gas_mpan in account_info['account']['properties'][0]['gasMeterPoints']:
            gas_mprn = kraken_gas_mpan['mprn']
            for kraken_gas_meter in kraken_gas_mpan['meters']:
                meters["Gas"].append({
                    "mpan":gas_mprn,
                    "serial":kraken_gas_meter['serialNumber'],
                    "mac":kraken_gas_meter["smartDevices"][0]["deviceId"]
                })
        return meters

    def load_account(self, account_name=None):
        if account_name:
            self.account = account_name

        self.account_config = self.get_account_config()

        # Check if access_token needs extending
        if 'access_key' in self.account_config and self.account_config['access_key'] != "":
            self.check_token_validity()
        else:
            self.get_new_access_token()
            self.account_config = self.get_account_config()

    def set_meter(self, meter_def):
        """
        <type>#<mpan>#<serial>#<mac>
        """
        self.meter_type, self.meter_mpan, self.meter_serial, self.meter_mac = meter_def.split("#")

    def collect_data(self, inputs, ew):
        pass

    def stream_events(self, inputs, ew):
        self.context_meta = inputs.metadata
        self.session_key = inputs.metadata['session_key']
        self.splunk_uri = inputs.metadata['server_uri']
        self.setup_util = Setup_Util(self.splunk_uri, self.session_key)
        self.collect_data(inputs,ew)

    @property
    def account_password(self):
        return self.account_config['account_password']

    def get_new_access_token(self):
        request = {
            'verify' : True,
            "headers" : {
                'Content-Type':  'application/json'
            },
            'payload' : {
                "operationName":"Login",
                "variables":{
                    "input":{
                        "email":self.account,
                        "password":self.account_password
                    }
                },
                "query":"mutation Login($input: ObtainJSONWebTokenInput!) { obtainKrakenToken(input: $input) { refreshExpiresIn refreshToken token } }"
            }
        }
        response = self.send_http_request("https://api.octopus.energy/v1/graphql/", "POST", **request)
        response_obj = response.json()
        refresh_token = response_obj['data']['obtainKrakenToken']['refreshToken']
        refreshExpiresIn = response_obj['data']['obtainKrakenToken']['refreshExpiresIn']
        access_token = response_obj['data']['obtainKrakenToken']['token']
        self.log_info(f"New token for account={self.account} refreshExpiresIn={refreshExpiresIn}")
        temp_account_conf = self.account_conf.get(self.account)
        try:
            del(temp_account_conf['eai:access'])
            del(temp_account_conf['disabled'])
        except:
            pass

        if 'account_number' not in temp_account_conf:
            self.log_info(f"Setting account_number for account={self.account}")
            temp_account_conf['account_number'] = self.get_kracken_user(access_token=access_token)['viewer']['accounts'][0]['number']

        temp_account_conf['access_token'] = access_token
        temp_account_conf['refresh_token'] = refresh_token
        self.account_conf.update(
            self.account,
            temp_account_conf,
            ["access_token","refresh_token","account_password"]
        )

        return response_obj

    def get_kracken_user(self, access_token=None):

        access_token = access_token if not access_token is None else self.account_config['access_token']

        request = {
            'verify' : True,
            "headers" : {
                'authorization' : access_token,
                'Content-Type':  'application/json'
            },
            'payload' : {
                "operationName":"GetUser",
                "variables":{},
                "query":"query GetUser { viewer { id preferredName givenName email accounts { number } } }"
            }
        }
        response = self.send_http_request("https://api.octopus.energy/v1/graphql/", "POST", **request)
        return response.json()['data']

    def get_kracken_account_info(self, access_token=None):
        access_token = access_token if not access_token is None else self.account_config['access_token']
        request = {
            'verify' : True,
            "headers" : {
                'authorization' : access_token,
                'Content-Type':  'application/json'
            },
            'payload' : {
                "operationName":"GetAccountInfo",
                "variables":{
                    "accountNumber":self.account_config['account_number'],
                    "propertiesActiveFrom":None
                },
                "query":"query GetAccountInfo($accountNumber: String!, $propertiesActiveFrom: DateTime) { account(accountNumber: $accountNumber) { accountType status number balance billingName canRequestRefund canRenewTariff projectedBalance shouldReviewPayments recommendedBalanceAdjustment ledgers { ledgerType balance } maximumRefund { amount reasonToRecommendAmount recommendedBalance } activeReferralSchemes { domestic { canBeReferred referralUrl referralDisplayUrl combinedRewardAmount referrerRewardAmount referredRewardAmount } business { canBeReferred referralUrl referralDisplayUrl combinedRewardAmount referrerRewardAmount referredRewardAmount } } paymentSchedules(first: 1, canCreatePayment: true) { edges { node { id paymentAmount paymentDay validTo validFrom isVariablePaymentAmount isPaymentHoliday reason totalDebtAmount } } } bills(first: 1, includeBillsWithoutPDF: false) { edges { node { id billType fromDate toDate issuedDate temporaryUrl } } } payments(first: 1) { edges { node { id amount paymentDate transactionType } } } properties(activeFrom: $propertiesActiveFrom) { id address postcode occupancyPeriods { effectiveFrom effectiveTo } coordinates { latitude longitude } smartDeviceNetworks { smartDevices { model deviceId status type paymentMode importElectricityMeter { id meterPoint { mpan } serialNumber installationDate consumptionUnits } gasMeter { meterPoint { mprn } id serialNumber installationDate consumptionUnits } } } electricityMeterPoints { __typename mpan id gspGroupId meters(includeInactive: false) { id serialNumber makeAndType meterType importMeter { id } smartDevices { paymentMode deviceId } prepayLedgers { creditLedger { currentBalance } debtLedger { currentBalance } } } agreements { id validFrom validTo tariff { __typename ... on TariffType { standingCharge preVatStandingCharge displayName fullName } ... on StandardTariff { unitRate preVatUnitRate } ... on DayNightTariff { dayRate preVatDayRate nightRate preVatNightRate } ... on HalfHourlyTariff { unitRates { value validFrom validTo } } } } enrolment { status supplyStartDate switchStartDate previousSupplier } smartStartDate smartTariffOnboarding { id lastUpdated latestStatus latestTermsStatus } status } gasMeterPoints { __typename mprn id meters { id serialNumber smartDevices { paymentMode deviceId } prepayLedgers { creditLedger { currentBalance } debtLedger { currentBalance } } } agreements { id validFrom validTo tariff { displayName fullName unitRate preVatUnitRate standingCharge preVatStandingCharge } } enrolment { status supplyStartDate switchStartDate previousSupplier } smartStartDate status } } } }"
            }
        }
        response = self.send_http_request("https://api.octopus.energy/v1/graphql/", "POST", **request)
        return response.json()['data']


    def get_live_usage(self,start_date="2023-04-04T20:09:56.930Z", end_date="2023-04-04T20:14:56.930Z"):

        request = {
            'verify' : True,
            "headers" : {
                'authorization' : self.account_config['access_token'],
                'Content-Type':  'application/json'
            },
            'payload' : {
                "operationName":"GetSmartMeterTelemetry",
                "variables":{
                    "deviceID": self.meter_mac,
                    "startDate": start_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    "endDate": end_date.strftime("%Y-%m-%dT%H:%M:%S"),
                    "grouping":"TEN_SECONDS"
                },
                "query":"""
                    query GetSmartMeterTelemetry($deviceID: String!, $startDate: DateTime!, $endDate: DateTime!, $grouping: TelemetryGrouping!) { 
                        smartMeterTelemetry(deviceId: $deviceID, start: $startDate, end: $endDate, grouping: $grouping) {
                            readAt consumption demand 
                        }
                    }"""
            }
        }
        try:
            response = self.send_http_request("https://api.octopus.energy/v1/graphql/", "POST", **request)
            jsonResp = response.json()
            if jsonResp and 'data' in jsonResp and 'smartMeterTelemetry' in jsonResp['data'] and jsonResp['data']['smartMeterTelemetry']:
                for resp in jsonResp['data']['smartMeterTelemetry']:
                    yield resp
            else:
                if 'errors' in jsonResp:
                    for error in jsonResp['errors']:
                        if 'Too many requests' in error['message']:
                            self.log_error("Too many requests to Octopus Energy API - Exiting")
                            exit()
                self.log_critical("No smartMeterTelemetry data received")
                self.log_warning(response.content)
        except(Exception) as e:
            import traceback
            import sys
            self.log_critical(traceback.format_exc())
            # or
            self.log_critical(sys.exc_info()[2])
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.log_critical(exc_type)
            self.log_critical(fname)
            self.log_critical(exc_tb.tb_lineno)

    def get_dispatches(self, type="planned"):

        request = {
            'verify' : True,
            "headers" : {
                'authorization' : self.account_config['access_token'],
                'Content-Type':  'application/json'
            },
            'payload' : {

                "operationName":f"{type}Dispatches",
                "variables":{
                    "accountNumber":self.account_config['account_number']
                },
                "query":f"""
                    query {type}Dispatches($accountNumber: String!) {{ 
                        {type}Dispatches(accountNumber: $accountNumber) {{
                            startDt, endDt
                        }}
                    }}"""
            }
        }

        try:
            response = self.send_http_request("https://api.octopus.energy/v1/graphql/", "POST", **request)
            jsonResp = response.json()
            if 'data' in jsonResp and f"{type}Dispatches" in jsonResp['data']:
                for resp in jsonResp['data'][f"{type}Dispatches"]:
                    yield resp
            else:
                self.log_info(f"No {type}Dispatches data received")
        except(Exception) as e:
            import traceback
            import sys
            self.log_critical(traceback.format_exc())
            # or
            self.log_critical(sys.exc_info()[2])
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.log_critical(exc_type)
            self.log_critical(fname)
            self.log_critical(exc_tb.tb_lineno)

    def get_account_config(self):
        accountName = self.account
        account_cfm = conf_manager.ConfManager(
            self.session_key,
            self.appName,
            realm=f"__REST_CREDENTIAL__#{self.appName}#configs/conf-{self.restPath}_account"
        )

        # Check if account is empty
        if not accountName:  # pylint: disable=E1101
            si.generateErrorResults("Enter OctopusEnergy account name.")
            raise Exception(
                "Account name cannot be empty. Enter a configured account name or "
                "create new account by going to Configuration page of the Add-on."
            )
        # Get account details

        self.log_info("Getting details for account '{}'".format(accountName) )
        self.account_conf = account_cfm.get_conf(
            f"{self.restPath}_account"
        )
        account_details = self.account_conf.get(accountName)

        return account_details

    def check_token_validity(self):
        #  Check that the access_token is valid
        decoded_jwt = jwt.decode(self.account_config['access_token'], options={"verify_signature": False})
        access_token_expiry = decoded_jwt['exp']
        if int(access_token_expiry) < int(time.time()):
            self.log_warning(f"Access token for account {self.account} has expired")
            new_creds = self.get_new_access_token()
            self.account_config['access_token'] = new_creds['data']['obtainKrakenToken']['token']
        else:
            self.log_debug("Access token has not expired")

    def get(self, accountName, url):
        self.account = accountName
        access_token = self.get_access_token()
        request = {
            'verify' : False,
            "headers" : {
                'Authorization': 'Bearer ' + access_token,
                'Content-Type':  'application/json'
            }
        }
        response = self.send_http_request(url, "GET", **request)
        try:
            response.raise_for_status()
        except:
            uri = f"{self.splunk_uri}/services/messages/new"
            headers = {}
            headers['Authorization'] = 'Splunk ' + self.session_key
            data ={'name':"OctopusEnergy App",'value':"Unable to renew auth token",'severity':"warn"}
            message_req = {
                "headers":headers,
                "payload":data,
                "verify":False
            }
            r = self.send_http_request(uri,"POST", headers=headers, payload=data, verify=False)
            if r.status_code<300:
                self.log_info("Logged message to UI")
            else:
                self.log_info(f"Resp from message API - status_code={r.status_code}")
            # {"error":"invalid bearer token"}
            self.log_warning("Error from API")
        return response
