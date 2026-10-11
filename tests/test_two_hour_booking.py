"""Consecutive hours are one atomic order; incomplete/mismatched selections stop."""
from dataclasses import asdict, replace
from datetime import datetime
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import booking_flow as flow
from mail_notice import payment_message
from poll_settings import MailSettings, PollSettings


def grid():
    return {'loading':False, 'unpaid_order_blocks_booking':False,
        'venue':{'venueId':'gym','venueName':'学生中心'},
        'active_type':{'isactiveName':'健身房'},'selected_date':{'isdate':'2026-10-18'},
        'fields':[{'fieldId':'field1','name':'场地1'},{'fieldId':'field2','name':'场地2'}],
        'slots':[{'field_index':index,'entries':[
            {'status':0,'can_select':True,'time_range':'18:00-19:00','price':'2'},
            {'status':0,'can_select':True,'time_range':'19:00-20:00','price':'3'}]} for index in range(2)]}


def target():
    return flow.candidates(grid(),'18:00-20:00')[0]


def checkout(slot):
    return {'total':slot.price,'dialog_visible':True,'terms_title':'预订须知','terms_text':'完整条款',
        'selected':[{'subSiteId':slot.field_id,'subSitename':slot.field,'scheduleTime':period,
            'venueNum':1,'venuePrice':price} for period,price in slot.parts()]}


def payload(slot):
    gate = flow.NetworkGate('simulate')
    gate.slot = slot
    return gate.simulated_order()


class TwoHourTests(unittest.TestCase):
    def test_same_field_both_hours_are_required_without_price_limit(self):
        selected = target()
        self.assertEqual((selected.time,selected.price,selected.hourly_prices),('18:00-20:00','5.00',('2.00','3.00')))
        self.assertEqual(flow.Slot(**json.loads(json.dumps(asdict(selected)))),selected)
        expensive = grid()
        for column in expensive['slots']:
            for entry in column['entries']: entry['price']='10001.00'
        self.assertEqual(flow.candidates(expensive,'18:00-20:00')[0].price,'20002.00')
        snapshot = grid()
        snapshot['slots'][0]['entries'][1].update(status=-1,can_select=False)
        snapshot['slots'][1]['entries'][0].update(status=-1,can_select=False)
        self.assertEqual(flow.candidates(snapshot,'18:00-20:00'),[])
        self.assertEqual(len(flow.candidates(snapshot,'18:00-19:00')),1)

    def test_gaps_invalid_lengths_and_missing_hour_prices_are_rejected(self):
        snapshot = grid()
        for column in snapshot['slots']:
            column['entries'][1]['time_range']='20:00-21:00'
        self.assertEqual(flow.candidates(snapshot,'18:00-20:00'),[])
        for period in ('18:00-21:00','19:00-18:00','23:00-00:00','18:30-20:30'):
            with self.subTest(period=period),self.assertRaises(flow.BookingError): flow.hourly_periods(period)
        with self.assertRaises(flow.BookingError): replace(target(),hourly_prices=()).parts()
        with self.assertRaises(flow.BookingError): replace(target(),hourly_prices=('2.00','2.00')).parts()
        configured=PollSettings.read({'booking':{'time_slots':['18:00-20:00'],'max_price':'0.01'}})
        self.assertEqual(configured.time_slots,('18:00-20:00',))
        self.assertFalse(hasattr(configured,'max_price'))
        self.assertNotIn('max_price',configured.public())

    def test_checkout_validates_both_hours_prices_and_single_field(self):
        slot = target()
        original = checkout(slot)
        original['selected'].reverse()
        flow.validate_selection(slot,original)
        variations = []
        missing = copy.deepcopy(original); missing['selected'].pop(); variations.append(missing)
        duplicate = copy.deepcopy(original); duplicate['selected'][1]=duplicate['selected'][0]; variations.append(duplicate)
        for key,value in [('subSiteId','field2'),('scheduleTime','20:00-21:00'),('venuePrice','2.50'),('venueNum',2)]:
            changed = copy.deepcopy(original); changed['selected'][0][key]=value; variations.append(changed)
        changed = copy.deepcopy(original); changed['total']='2'; variations.append(changed)
        for index,value in enumerate(variations):
            with self.subTest(index=index),self.assertRaises(flow.BookingError): flow.validate_selection(slot,value)

    def test_server_order_must_confirm_each_hour_even_if_total_is_correct(self):
        slot = target()
        original = payload(slot)
        original['data']['spaces'].reverse()
        self.assertEqual(flow.validate_order(slot,original)['amount_due'],'5.00')
        for mode in ('missing','duplicate','field','time','price','date','total'):
            changed = copy.deepcopy(original)
            spaces = changed['data']['spaces']
            if mode=='missing': spaces.pop()
            elif mode=='duplicate': spaces[1]=copy.deepcopy(spaces[0])
            elif mode=='field': spaces[0]['subSiteId']='field2'
            elif mode=='time': spaces[0]['scheduleTime']='20:00-21:00'
            elif mode=='price': spaces[0]['venuePrice']='2.50'
            elif mode=='date': spaces[0]['scheduleDate']='2026-10-19'
            else: changed['data']['countPrice']='2.00'
            with self.subTest(mode=mode),self.assertRaises(flow.BookingError): flow.validate_order(slot,changed)

    def test_confirmation_binds_individual_hour_prices_not_only_the_total(self):
        original={'schema_version':1,'slot':asdict(target()),'terms_sha256':'terms'}
        changed=copy.deepcopy(original)
        changed['slot']['hourly_prices']=('3.00','2.00')
        self.assertNotEqual(flow.confirmation_hash(original),flow.confirmation_hash(changed))

    def test_confirmed_hour_blocks_overlapping_two_hour_order_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'journal.json'
            first=flow.candidates(grid(),'18:00-19:00')[0]
            journal=flow.Journal(path)
            entry=journal.begin('account',first,[])
            journal.update(entry,'confirmed')
            # Legacy records did not contain hourly_prices; duplicate protection survives.
            entry['slot'].pop('hourly_prices'); journal.save()
            with self.assertRaises(flow.BookingError): flow.Journal(path).assert_can_submit('account',target())
            journal.assert_can_submit('other-account',target())
            journal.assert_can_submit('account',replace(first,time='19:00-20:00',price='3.00'))

    def test_stage_clicks_two_original_cells_then_validates_before_terms(self):
        slot=target()
        with patch.object(flow,'LoginBrowser'): browser=flow.BookingBrowser({})
        browser.account='account'
        browser.grid=MagicMock(return_value=grid())
        browser.page=MagicMock()
        cells=browser.page.locator.return_value
        cells.count.return_value=4
        browser.page.evaluate.return_value=checkout(slot)
        prepared=browser.stage(slot)
        self.assertEqual([call.args[0] for call in cells.nth.call_args_list],[0,2])
        self.assertEqual(prepared['slot']['time'],'18:00-20:00')
        self.assertEqual(prepared['slot']['price'],'5.00')
        self.assertEqual(cells.nth.return_value.click.call_count,2)

    def test_one_hour_only_selected_by_site_stops_before_opening_terms(self):
        slot=target()
        with patch.object(flow,'LoginBrowser'): browser=flow.BookingBrowser({})
        browser.grid=MagicMock(return_value=grid());browser.page=MagicMock()
        browser.page.locator.return_value.count.return_value=4
        incomplete=checkout(slot);incomplete['selected'].pop();browser.page.evaluate.return_value=incomplete
        with self.assertRaises(flow.BookingError): browser.stage(slot)
        browser.page.get_by_role.assert_not_called()

    def test_email_contains_full_two_hour_period_duration_and_order_total(self):
        slot=target()
        settings=MailSettings('smtp.example.test',465,'ssl','from@example.test','local-code','from@example.test','to@example.test')
        record={'order':{'slot':asdict(slot),'confirmed_at':'2026-10-11T04:00:00+00:00',
            'amount_due':'5.00','order_id':'TEST-TWO-HOUR'},'message_id':'<test@example.test>',
            'created_at':'2026-10-11T04:00:00+00:00'}
        message=payment_message(settings,record)
        self.assertIn('18:00-20:00',message['Subject'])
        self.assertIn('时长：2小时',message.get_content())
        self.assertIn('应付金额：5.00元',message.get_content())


if __name__=='__main__': unittest.main()
