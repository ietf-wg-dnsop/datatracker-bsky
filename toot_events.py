#!/usr/bin/env python3

import argparse
import json
import os
import sys

import requests

try:
    import mastodon
except ImportError:
    mastodon = None

# This should be added to the Org's secret list
#TOOT_API_BASE = "https://hachyderm.io/"

class DatatrackerTracker:
    API_BASE = "https://datatracker.ietf.org"
    CHUNK_SIZE = 250
    REQ_LIMIT = 20
    RETRY_MAX = 2
    RETRY_DELAY = 30
    INTERESTING_EVENTS = {
        "iesg_approved": "The IESG has approved {title} for publication as an RFC. {link}",
        "new_revision": "New revision {rev} of {title} published. {link}",
        "published_rfc": "{title} has been published as an RFC. {link}",
        "sent_last_call": "IETF Last Call for {title} has started. {link}",
        "started_iesg_process": "{title} has entered evalutation by the IESG. {link}",
        "changed_state": {
            "IETF WG state changed to <b>In WG Last Call</b> from WG Document": "{title} is now in Working Group Last Call. {link}"
        },
    }

    def __init__(self, argv=None):
        self.args = self.parse_args(argv)
        self.toot_api = None
        self.toot_hash = os.environ.get('MY_HOME', " ")

    def run(self):
        last_seen_id = self.get_last_seen()
        self.note(f"Resuming at event: {last_seen_id}")
        if self.args.markdown:
            print(f"### Tooting datatracker events since #{last_seen_id}")
        events = self.get_events(last_seen_id)
        new_last_seen = self.process_events(events, last_seen_id)
        self.note(f"Last event seen: {new_last_seen}")
        self.write_last_seen(new_last_seen)

    def process_events(self, events, last_seen_id):
        num = 0
        for event in events:
            last_seen_id = event["id"]
            if not f"draft-ietf-{self.args.wg}" in event["doc"]:
                continue
            if self.args.debug:
                self.note(f"Event: {event['type']} ({event['desc']})")
            template = self.INTERESTING_EVENTS.get(event["type"], None)
            if type(template) is dict:
                template = template.get(event["desc"], None)
            if not template:
                continue
            try:
                message = self.format_message(event, template)
            except ValueError:
                break
            self.note(f"Message: {self.toot_hash} {message}")
            if self.args.markdown:
                print(f"* {self.toot_hash} {message}")
            if not self.args.dry_run:
                try:
                    self.toot(f"{self.toot_hash} {message}")
                except mastodon.MastodonError:
                    last_seen_id = event["id"] - 1
                    break  # didn't tweet so we should bail
            num += 1
        if self.args.markdown:
            print(f"Found {num} events.")
        return last_seen_id

    def get_events(self, last_seen_id=None):
        results = self.get_doc(
            f"/api/v1/doc/docevent/?format=json&limit={self.CHUNK_SIZE}"
        )
        events = results["objects"]
        events.reverse()
        if last_seen_id is None:
            return events
        req = 0
        while (
            last_seen_id not in [event["id"] for event in events]
            and req < self.REQ_LIMIT
        ):
            req += 1
            next_link = results["meta"].get("next", None)
            results = self.get_doc(next_link)
            more_events = results["objects"]
            more_events.reverse()
            events[:0] = more_events
        if req >= self.REQ_LIMIT:
            self.warn(f"Max requests reached: {req}")
        new_events = [event for event in events if event["id"] > last_seen_id]
        if len(new_events) == len(events) and last_seen_id is not None:
            self.warn(f"Event ID {last_seen_id} not found.")
        return new_events

    def format_message(self, event, template):
        doc = self.get_doc(event["doc"])
        title = doc["title"]
        name = doc["name"]
        ev_id = event["id"]
        rev = doc.get("rev", "")
        link = f"{self.API_BASE}/doc/{name}/"
        return template.format(**locals())

    def init_mastodon(self):
        try:
            self.toot_api = mastodon.Mastodon(
                                    access_token=os.environ["TOOT_TOKEN_KEY"],
                                    api_base_url=os.environ["TOOT_API_BASE"],
                                    ratelimit_method='wait')
        except mastodon.MastodonError as why:
            print(why)


    def toot(self, message):
        if self.toot_api is None:
            self.init_mastodon()
        try:
            status = self.toot_api.toot(message)
        except mastodon.MastodonError as why:
            print(why)


    def parse_args(self, argv):
        parser = argparse.ArgumentParser(
            description="Toot about recent changes in IETF Working Groups"
        )
        parser.add_argument(
            "-g",
            "--group",
            dest="wg",
            required=True,
            help="Working Group's short name; e.g., 'tls', 'httpbis'",
        )
        parser.add_argument(
            "-d",
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="don't toot; just show messages on STDOUT",
        )
        parser.add_argument(
            "-m",
            "--markdown",
            dest="markdown",
            action="store_true",
            help="output status in markdown format",
        )
        parser.add_argument(
            "--debug",
            dest="debug",
            action="store_true",
            help="Debug mode. Implies --dry-run.",
        )
        parser.add_argument(
            "-l",
            "--last-seen",
            dest="last_seen_id",
            type=int,
            default=None,
            help="last event ID seen",
        )
        parser.add_argument(
            "-f",
            "--file",
            dest="last_seen_file",
            help="file to read last seen ID from and write it back to after processing",
        )
        return parser.parse_args(argv)

    def get_last_seen(self):
        last_seen_id = None
        if self.args.last_seen_id is not None:
            last_seen_id = self.args.last_seen_id
        elif self.args.last_seen_file:
            try:
                with open(self.args.last_seen_file, encoding="ascii") as fh:
                    last_seen_id = int(fh.read())
            except IOError as why:
                self.warn(f"Cannot open {self.args.last_seen_file} for reading: {why}")
                sys.exit(1)
            except ValueError as why:
                self.error(f"Last seen file does not contain an integer: {why}")
        return last_seen_id

    def write_last_seen(self, last_seen_id):
        if self.args.last_seen_file:
            try:
                with open(self.args.last_seen_file, "w", encoding="ascii") as fh:
                    fh.write(str(last_seen_id))
            except IOError as why:
                self.error(f"Cannot open {self.args.last_seen_file} for writing: {why}")

    def get_doc(self, doc_url):
        self.note(f"Fetching: <{doc_url}>")
        try:
            req = requests.get(self.API_BASE + doc_url, timeout=15)
        except requests.exceptions.RequestException as why:
            self.warn(f"Request exception for <{doc_url}>: {why}")
            raise ValueError
        if req.status_code != 200:
            self.warn(f"Status code {req.status_code} from <{doc_url}>")
            raise ValueError
        try:
            return req.json()
        except json.decoder.JSONDecodeError as why:
            self.warn(f"JSON parse error from <{doc_url}>: {why}")
            raise ValueError

    def note(self, message):
        sys.stderr.write(f"{message}\n")

    def warn(self, message):
        sys.stderr.write(f"WARNING: {message}\n")
        if self.args.markdown:
            print(f"\n`Warning` {message}\n")

    def error(self, message):
        sys.stderr.write(f"ERROR: {message}\n")
        if self.args.markdown:
            print("## Error")
            print(f"{message}")
        sys.exit(1)


if __name__ == "__main__":
    DatatrackerTracker().run()
