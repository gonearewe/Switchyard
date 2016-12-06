'''
OF switch unit tests.
'''

import unittest
from unittest.mock import Mock, MagicMock

from switchyard.lib.address import *
from switchyard.lib.packet import *
from switchyard.lib.interface import Interface, make_device_list
from switchyard.lib.testing import TestScenario, SwitchyTestEvent
from switchyard.lib.exceptions import *
from switchyard.llnettest import LLNetTest, _prepare_debugger
from switchyard.llnetreal import LLNetReal
from switchyard.llnetbase import LLNetBase
import switchyard.llnetreal as llreal

class WrapLLNet(LLNetReal):
    def __init__(self, devlist, name=None):
        LLNetBase.__init__(self)
        # don't call up to LLNetBase; avoid any actual pcap stuff
        
    def _fix_devinfo(self, dlist):
        self.devinfo = {}
        for i,d in enumerate(dlist):
            self.devinfo[d] = Interface(d, EthAddr('00:00:00:00:00:00'), IPAddr(i), '255.255.255.255', i)



class LLNetDevTests(unittest.TestCase):
    def setUp(self):
        self.scenario = TestScenario('test')
        self.scenario.add_interface('eth1', '11:11:11:11:11:11')
        self.scenario.add_interface('eth0', '00:00:00:00:00:00')
        self.scenario.add_interface('eth2', '22:22:22:22:22:22')
        self.scenario.add_interface('eth7', '77:77:77:77:77:77', ipaddr='192.168.1.1', netmask='255.255.255.0')
        self.scenario.done = Mock(return_value=False)
        self.ev = Mock()
        self.ev.match = Mock(return_value=None)
        self.scenario.next = Mock(return_value=self.ev)
        self.fake = LLNetTest(self.scenario)

        self.devs = make_device_list([], [])
        self.real = WrapLLNet(self.devs)
        self.real._fix_devinfo(self.devs)
        self.real._pcaps = Mock()
        self.real._pcaps.get = Mock(return_value=Mock())

    def testFakeSendDevName(self):
        p = Packet()
        self.fake.send_packet("eth1", p)
        self.ev.match.assert_called_with(SwitchyTestEvent.EVENT_OUTPUT, device='eth1', packet=p)

    def testFakeSendDevNum(self):
        p = Packet()
        self.fake.send_packet(0, p)
        self.ev.match.assert_called_with(SwitchyTestEvent.EVENT_OUTPUT, device='eth1', packet=p)
        self.fake.send_packet(3, p)
        self.ev.match.assert_called_with(SwitchyTestEvent.EVENT_OUTPUT, device='eth7', packet=p)

    def testModeResult(self):
        self.assertTrue(self.fake.testmode)
        self.assertFalse(self.real.testmode)

    def testFakeSendIntfObj(self):
        p = Packet()
        ifmap = self.scenario.interfaces()
        self.fake.send_packet(ifmap['eth1'], p)
        self.ev.match.assert_called_with(SwitchyTestEvent.EVENT_OUTPUT, device='eth1', packet=p)
        self.fake.send_packet(ifmap['eth2'], p)
        self.ev.match.assert_called_with(SwitchyTestEvent.EVENT_OUTPUT, device='eth2', packet=p)

    def testRealSendDevName(self):
        p = Packet()
        for d in self.devs:
            self.real.send_packet(d, p)
            self.real._pcaps.get.assert_called_with(d, None)

    def testRealSendDevNum(self):
        p = Packet()
        for d,intf in self.real.devinfo.items():
            self.real.send_packet(intf.name, p)
            self.real._pcaps.get.assert_called_with(intf.name, None)

    def testRealSendIntfObj(self):
        p = Packet()
        for d,intf in self.real.devinfo.items():
            self.real.send_packet(intf, p)
            self.real._pcaps.get.assert_called_with(intf.name, None)

    def testFakeCallback(self):
        called = (None,None)

        def cb(intf, updown):
            nonlocal called
            called = (intf,updown)

        self.fake.set_devupdown_callback(cb)
        self.fake.intf_down('eth7')
        self.assertEqual(called[1], 'down')
        self.assertEqual(called[0].name, 'eth7')

        with self.assertRaises(SwitchyException):
            self.fake.intf_up(self.fake.interface_by_name('eth0'))
        self.assertEqual('test', self.fake.name)
        self.fake.intf_up(Interface("testif", "00:00:00:11:11:11", "1.2.3.4"))

    def testFakeAddrLookups(self):
        with self.assertRaises(SwitchyException):
            self.fake.interface_by_name('eth9')

        intf = self.fake.interface_by_macaddr('11:11:11:11:11:11')
        self.assertEqual(intf.name, 'eth1')
        intf = self.fake.port_by_macaddr('11:11:11:11:11:11')
        self.assertEqual(intf.name, 'eth1')

        with self.assertRaises(SwitchyException):
            intf = self.fake.interface_by_macaddr('11:11:11:11:11:99')

        with self.assertRaises(SwitchyException):
            intf = self.fake.port_by_macaddr('11:11:11:11:11:99')

        intf = self.fake.interface_by_ipaddr('192.168.1.1')
        self.assertEqual(intf.name, 'eth7')

        intf = self.fake.port_by_ipaddr('192.168.1.1')
        self.assertEqual(intf.name, 'eth7')

        with self.assertRaises(SwitchyException):
            intf = self.fake.interface_by_ipaddr('192.168.0.1')

        with self.assertRaises(SwitchyException):
            intf = self.fake.port_by_ipaddr('192.168.0.1')

        with self.assertRaises(SwitchyException):
            self.fake._lookup_devname(99)

        intf = self.fake._lookup_devname(0)
        self.assertEqual(intf, 'eth1')

    def testFakeDebugger(self):
        try:
            1/0
        except:
            import sys
            t,v,tb = sys.exc_info()
            # print(dir(tb))
            p = _prepare_debugger(tb)
            # print(p)
            import pdb
            self.assertIsInstance(p, pdb.Pdb)

    def testReal(self):
        import signal
        si = signal.SIGINT
        setattr(signal, "signal", Mock())

        mdev = Mock(return_value=[])
        setattr(LLNetReal, "__assemble_dev_info", mdev)

        mock_pcap = MagicMock()
        setattr(llreal, "PcapLiveDevice", mock_pcap)
        mthreads = Mock()
        setattr(LLNetReal, "__spawn_threads", mthreads)

        lr = LLNetReal(['en0'], "testy") 
        self.assertEqual(lr.name, "testy")
        with self.assertRaises(SwitchyException):
            lr.send_packet("baddev", Packet())
        with self.assertRaises(SwitchyException):
            lr.send_packet("en0", b'\xde\xad')
        with self.assertRaises(SwitchyException):
            lr.send_packet("en0", None)

        lr._sig_handler(si, None)
        lr.shutdown()
        self.assertIn('en0', lr._pcaps)

        mdev.assert_not_called()
        mthreads.assert_not_called()
        mock_pcap.assert_called_with('en0')
        self.assertFalse(lr._pktqueue.empty())
        with self.assertRaises(Shutdown):
            lr.recv_packet()

        lr = LLNetReal(['en0'], "testy") 
        lr.shutdown()
        self.assertFalse(LLNetReal.running)


if __name__ == '__main__':
    unittest.main()
