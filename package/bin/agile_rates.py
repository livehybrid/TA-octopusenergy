import import_declare_test
import sys
import json
import datetime

from splunklib import modularinput as smi
from splunktaucclib.modinput_wrapper.base_modinput import BaseModInput
from splunktaucclib.splunk_aoblib.setup_util import Setup_Util
from octopus_modinput import OCTOPUS_API_BASE
from octopus_paging import iter_pages, parse_api_time, utc_iso

# Days requested per run. Every page is followed, so this only bounds how far
# one run reaches; future slots that are not published yet come back empty.
WINDOW_DAYS = 7
LEGACY_CHECKPOINT_KEY = "start_time"

class AGILE_RATES(BaseModInput):

    def __init__(self):
        super(AGILE_RATES, self).__init__("TA-octopusenergy", "agile_rates", False)

    def get_app_name(self):
        # BaseModInput.get_app_name is abstract and does `raise NotImplemented`,
        # which is itself a bug in the vendored library: NotImplemented is not an
        # exception, so calling it dies with "TypeError: exceptions must derive
        # from BaseException" rather than a useful message. It is reached via the
        # `app` property, which _init_ckpt() uses to name the KV store
        # checkpointer, so any collector that checkpoints must override this.
        return "TA-octopusenergy"

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
        # BaseModInput.stream_events normally does this, but we override it and
        # never call super(), so context_meta stayed at its {} default from
        # BaseModInput.__init__. _init_ckpt() reads server_uri/session_key off
        # context_meta, so the first get_check_point() below raised
        # "server_uri not found in input meta." and the input died before any
        # API call. octopus_modinput.py's collectors were unaffected because
        # their stream_events does set it.
        self.context_meta = inputs.metadata
        uri = inputs.metadata["server_uri"]
        session_key = inputs.metadata['session_key']
        self.setup_util = Setup_Util(uri, session_key, self.logger)
        for input_name, input_item in inputs.inputs.items():
            self.collect_input(input_name, input_item, ew)

    def load_start(self, input_name, input_item):
        """Where this input resumes, as a naive UTC datetime.

        Each input has its own checkpoint. Before 1.1 every agile input shared
        one key, 'start_time', so a second input resumed from the first one's
        position and skipped its own range. The first input to run after an
        upgrade takes that shared position over, so an existing input carries
        on where it was.
        """
        key = agile_checkpoint_key(input_name)
        start_time = self.get_check_point(key)
        if not start_time:
            start_time = self.get_check_point(LEGACY_CHECKPOINT_KEY)
            if start_time:
                # Claim it: save it under this input's key and drop the shared
                # one, so an input added later starts from its own start_date.
                self.save_check_point(key, start_time)
                self.delete_check_point(LEGACY_CHECKPOINT_KEY)
                self.log_info(f"Carried over shared checkpoint start_time={start_time} for {input_name}")
        if not start_time:
            # start_date is optional (no default), so an input without one
            # starts at the beginning of today.
            start_time = input_item.get('start_date')
        if not start_time:
            return datetime.datetime.combine(datetime.datetime.now(datetime.timezone.utc).date(), datetime.time(0, 0))
        start_time = str(start_time).strip()
        if '.' not in start_time:
            start_time = start_time + '.0'
        return datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S.%f")

    def collect_input(self, input_name, input_item, ew):
        """Fetch every published rate from the checkpoint onwards.

        The window is WINDOW_DAYS from the checkpoint and every page is
        followed. Rates are published up to 23:00 the next day, so the window
        normally ends in empty future slots; the checkpoint moves to the end of
        the newest slot written, and the next run starts exactly there. Before
        this the input reset its start to "now" after 16:00 and fetched the
        same day again on every run, writing each slot many times over.
        """
        rate_code = input_item['rate_code']
        start_dt = self.load_start(input_name, input_item)
        end_dt = start_dt + datetime.timedelta(days=WINDOW_DAYS)
        self.log_warning(f"start_time={start_dt}")
        url = "{}/v1/products/{}/electricity-tariffs/E-1R-{}-M/standard-unit-rates/?period_from={}&period_to={}".format(
            OCTOPUS_API_BASE, rate_code, rate_code, utc_iso(start_dt), utc_iso(end_dt))
        self.log_info(f"Getting rates for {start_dt} to {end_dt}")

        def fetch(page_url):
            self.log_debug(f"Getting url={page_url}")
            response = self.send_http_request(page_url, "GET", parameters=None, payload=None,
                                              headers=None, cookies=None, verify=True, cert=None,
                                              timeout=30, use_proxy=False)
            response.raise_for_status()
            return response.json()

        seen = set()
        newest_end = None
        pages = 0
        for page, rate in iter_pages(fetch, url, api_base=OCTOPUS_API_BASE):
            pages = page
            slot = rate.get('valid_from')
            if slot in seen:
                # A slot published between two page requests shifts the
                # newest-first pages by one, so one rate can appear twice.
                continue
            seen.add(slot)
            event = self.new_event(time=slot, host="localhost", index=input_item['index'],
                                   source=input_name, sourcetype="octopusenergy:agile_rates",
                                   data=json.dumps(rate))
            ew.write_event(event)
            valid_to = rate.get('valid_to')
            if valid_to:
                end = parse_api_time(valid_to)
                if newest_end is None or end > newest_end:
                    newest_end = end
        self.log_info(f"Wrote {len(seen)} rates from {pages} page(s)")
        if newest_end is not None:
            checkpoint = newest_end.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            self.save_check_point(agile_checkpoint_key(input_name), checkpoint)


def agile_checkpoint_key(input_name):
    # No '/' in the key: Splunk 10.6.0.5 404s a by-id read of a KV _key with '://'.
    return "{}_start_time".format(input_name.replace("://", "__"))


if __name__ == '__main__':
    exit_code = AGILE_RATES().run(sys.argv)
    sys.exit(exit_code)