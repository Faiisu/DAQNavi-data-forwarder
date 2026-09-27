"""Device scan reports installed hardware without inventing devices."""

import importlib
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

web = importlib.import_module('web.app')

ENUM_OUTPUT = """+---------+---------------+-------------------------------+
| No.#    | Device Number | Device Description            |
+---------+---------------+-------------------------------+
| 0       | 0             | PCI-1716,BID#0                |
+---------+---------------+-------------------------------+
| Total: 1 devices                                        |
"""


class DeviceScanTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        cookie = web.create_test_session('operator')
        self.client.set_cookie(web.COOKIE_NAME, cookie)

    @patch('serial.tools.list_ports.comports', return_value=[])
    @patch.object(web.subprocess, 'run')
    def test_scan_reports_pci_card_from_advantech_enumerator(self, run, _ports):
        run.return_value = subprocess.CompletedProcess([], 0, ENUM_OUTPUT, '')

        response = self.client.get('/api/scan_usb')

        self.assertEqual(response.status_code, 200)
        devices = response.get_json()['devices']
        self.assertEqual([device['id'] for device in devices], ['PCI-1716,BID#0'])
        self.assertTrue(devices[0]['is_daq'])
        self.assertEqual(devices[0]['port'], 'BID#0')
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['/opt/advantech/tools/dev_enum'])

    @patch('serial.tools.list_ports.comports', return_value=[])
    @patch.object(web.subprocess, 'run')
    def test_empty_enumeration_does_not_report_demo_hardware(self, run, _ports):
        run.return_value = subprocess.CompletedProcess([], 0, '| Total: 0 devices |', '')

        response = self.client.get('/api/scan_usb')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['devices'], [])

    @patch('serial.tools.list_ports.comports', return_value=[])
    @patch.object(web.subprocess, 'run', side_effect=FileNotFoundError())
    def test_missing_enumerator_reports_scan_failure(self, _run, _ports):
        response = self.client.get('/api/scan_usb')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['devices'], [])
        self.assertIn('Advantech DAQ scan failed', response.get_json()['warnings'][0])

    @patch('serial.tools.list_ports.comports')
    @patch.object(web.subprocess, 'run')
    def test_serial_ports_remain_visible_alongside_pci_card(self, run, ports):
        run.return_value = subprocess.CompletedProcess([], 0, ENUM_OUTPUT, '')
        ports.return_value = [SimpleNamespace(
            device='/dev/ttyUSB0', description='USB adapter', manufacturer='Example', hwid='USB:1234')]

        response = self.client.get('/api/scan_usb')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([device['id'] for device in response.get_json()['devices']],
                         ['PCI-1716,BID#0', '/dev/ttyUSB0'])


if __name__ == '__main__':
    unittest.main()
