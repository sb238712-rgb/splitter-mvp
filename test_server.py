"""Unit tests: python -m unittest test_server.py -v"""
import os
import tempfile
import unittest
from pathlib import Path

import server


class TestSplitter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(cls.tmp.name) / 'test.sqlite3'
        server.PUBLIC_URL = 'http://test.local:8765'
        server.setup()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def create(self, owner_balance=50000, shares=(2000, 2000)):
        # pepperoni 24.90 + delivery 4.90 = 29.80 BYN => 2980 cents
        items = [{'sku': 'pepperoni', 'qty': 1}]
        # shares must total 2980
        return server.create_session({
            'items':items,
            'owner_amount_cents':2980-sum(shares),
            'owner_balance_cents':owner_balance,
            'participants':[
                {'name': 'Лена', 'contact_masked': 'Телефон ···· 1234', 'amount_cents': shares[0]},
                {'name': 'Паша', 'contact_masked': 'Карта ···· 9876', 'amount_cents': shares[1]},
            ]})

    def test_price_is_calculated_by_server(self):
        cart=server.cart_from_rows([{'sku':'pepperoni','qty':1}])
        self.assertEqual(cart['total_cents'],2980)
        self.assertEqual(server.cart_from_rows([{'sku':'fourcheese','qty':3}])['delivery_cents'],0)

    def test_everyone_approves_only_then_success(self):
        room=self.create(shares=(980,1000))
        first, second=[p['token'] for p in room['participants']]
        res=server.respond(first,'approve')
        self.assertEqual(res['session_status'],'pending')
        res=server.respond(second,'approve')
        self.assertEqual(res['session_status'],'success')
        self.assertEqual(sum([p['amount_cents'] for p in room['participants']])+room['owner_amount_cents'],2980)
        # second request cannot charge twice
        self.assertEqual(server.respond(second,'approve')['session_status'],'success')

    def test_decline_cancels_entire_operation(self):
        room=self.create(shares=(1000,1000))
        first,second=[p['token'] for p in room['participants']]
        server.respond(first,'approve')
        self.assertEqual(server.respond(second,'reject')['session_status'],'cancelled')
        with server.conn() as db:
            s=server.get_session(db,room['owner_token'])
            self.assertEqual(s['ledger_json'],'[]')

    def test_simulated_insufficient_cancels(self):
        room=self.create(shares=(1000,1000))
        self.assertEqual(server.respond(room['participants'][0]['token'],'insufficient')['session_status'],'cancelled')

    def test_owner_insufficient_cancels(self):
        room=self.create(owner_balance=1,shares=(1000,1000))
        for token in [p['token'] for p in room['participants']]:server.respond(token,'approve')
        with server.conn() as db:
            self.assertEqual(server.get_session(db,room['owner_token'])['status'],'cancelled')

    def test_invalid_total_or_full_pan_cannot_be_sent(self):
        with self.assertRaises(ValueError):
            self.create(shares=(2000,2000))  # owner amount becomes negative
        data={'items':[{'sku':'pepperoni','qty':1}], 'owner_amount_cents':980,
              'participants':[{'name':'Тест','contact_masked':'1234567890123456','amount_cents':2000}]}
        with self.assertRaises(ValueError): server.validate_session(data)

    def test_owner_cancel(self):
        room=self.create(shares=(1000,1000))
        result=server.cancel_owner(room['owner_token'])
        self.assertEqual(result['status'],'cancelled')


if __name__=='__main__': unittest.main()
