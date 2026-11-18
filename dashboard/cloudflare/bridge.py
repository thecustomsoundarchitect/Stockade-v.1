#!/usr/bin/env python3
"""Send the local Stockade status to an Access protected Worker."""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import snapshot


def main():
    url = os.environ.get('STOCKADE_WORKER_URL', '').rstrip('/')
    client_id = os.environ.get('CF_ACCESS_CLIENT_ID', '')
    client_secret = os.environ.get('CF_ACCESS_CLIENT_SECRET', '')
    token = os.environ.get('STOCKADE_BRIDGE_TOKEN', '')
    data_dir = Path(os.environ.get('STOCKADE_DATA_DIR', str(Path(__file__).resolve().parents[2] / 'data'))).expanduser().resolve()
    if urlparse(url).scheme != 'https' or not all((client_id, client_secret, token)):
        raise SystemExit('The Worker address and Access credentials are required.')
    sent_report = None
    while True:
        state = snapshot(data_dir)
        reports = state.get('sync_reports', [])
        if sent_report:
            for index, report in enumerate(reports):
                if report['id'] == sent_report:
                    state['sync_reports'] = reports[index:]
                    break
        body = json.dumps(state).encode('utf-8')
        request = Request(url + '/api/publish', data=body, method='POST', headers={
            'Content-Type': 'application/json',
            'CF-Access-Client-Id': client_id,
            'CF-Access-Client-Secret': client_secret,
            'X-Stockade-Bridge-Token': token,
        })
        try:
            with urlopen(request, timeout=15) as response:
                if response.status != 204:
                    print('Could not send dashboard status.', file=sys.stderr, flush=True)
                else:
                    sent_report = response.headers.get('X-Stockade-Report-Id') or sent_report
        except OSError:
            print('Could not send dashboard status.', file=sys.stderr, flush=True)
        time.sleep(30)


if __name__ == '__main__':
    main()
