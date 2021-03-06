from bs4 import BeautifulSoup
from cybersource.constants import CHECKOUT_BASKET_ID, CHECKOUT_ORDER_NUM, CHECKOUT_SHIPPING_CODE, CHECKOUT_ORDER_ID
from cybersource.tests import factories as cs_factories
from decimal import Decimal as D
from django.core import mail
from django.core.urlresolvers import reverse
from mock import patch
from oscar.core.loading import get_class, get_model
from oscar.test import factories
from random import randrange
from rest_framework.test import APITestCase
import datetime
import requests # Needed for external calls!

Basket = get_model('basket', 'Basket')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')


class BaseCheckoutTest(APITestCase):
    fixtures = ['cybersource-test.yaml']

    def create_product(self, price=D('10.00')):
        product = factories.create_product(
            title='My Product',
            product_class='My Product Class')
        record = factories.create_stockrecord(
            currency='USD',
            product=product,
            num_in_stock=10,
            price_excl_tax=price)
        factories.create_purchase_info(record)
        return product

    def do_add_to_basket(self, product_id, quantity=1):
        url = reverse('api-basket-add-product')
        data = {
            "url": reverse('product-detail', args=[product_id]),
            "quantity": quantity
        }
        return self.client.post(url, data)

    def do_get_basket(self):
        url = reverse('api-basket')
        return self.client.get(url)

    def do_sign_auth_request(self, basket_id=None, data=None):
        if data is None:
            data = {
                "guest_email": "herp@example.com",
                "basket": reverse('basket-detail', args=[basket_id]),
                "shipping_address": {
                    "first_name": "fadsf",
                    "last_name": "fad",
                    "line1": "234 5th Ave",
                    "line4": "Manhattan",
                    "postcode": "10001",
                    "state": "NY",
                    "country": reverse('country-detail', args=['US']),
                    "phone_number": "+1 (717) 467-1111",
                }
            }
        url = reverse('cybersource-sign-auth-request')
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, 200)

        next_year = datetime.date.today().year + 1
        cs_data = {
            'card_type': '001',
            'card_number': '4111111111111111',
            'card_cvn': '123',
            'card_expiry_date': '12-{}'.format(next_year),
            'bill_to_forename': 'Testy',
            'bill_to_surname': 'McUnitTest',
            'bill_to_address_line1': '234 5th Ave',
            'bill_to_address_line2': 'apt 5',
            'bill_to_address_city': 'Manhattan',
            'bill_to_address_state': 'NY',
            'bill_to_address_postal_code': '10001',
            'bill_to_address_country': 'US',
            'bill_to_phone': '17174671111',
        }
        for field in res.data['fields']:
            if not field['editable'] or field['key'] not in cs_data:
                cs_data[field['key']] = field['value']
        cs_url = res.data['url']
        return cs_url, cs_data

    def do_cybersource_post(self, cs_url, cs_data):
        res = requests.post(cs_url, cs_data)
        self.assertEqual(res.status_code, 200)

        soup = BeautifulSoup(res.content, 'html.parser')
        form_data = {}
        for element in soup.find_all('input'):
            form_data[element['name']] = element['value']

        # We have the data from cybersource, send it to our cybersource callback
        url = reverse('cybersource-reply')
        return self.client.post(url, form_data)

    def check_finished_order(self, number, product_id, quantity=1):
        # Order exists and was paid for
        self.assertEqual(Order.objects.all().count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.number, number)

        lines = order.lines.all()
        self.assertEqual(lines.count(), 1)
        line = lines[0]
        self.assertEqual(line.quantity, quantity)
        self.assertEqual(line.product_id, product_id)

        payment_events = order.payment_events.filter(event_type__name="Authorise")
        self.assertEqual(payment_events.count(), 1)
        self.assertEqual(payment_events[0].amount, order.total_incl_tax)

        payment_sources = order.sources.all()
        self.assertEqual(payment_sources.count(), 1)
        self.assertEqual(payment_sources[0].currency, order.currency)
        self.assertEqual(payment_sources[0].amount_allocated, order.total_incl_tax)
        self.assertEqual(payment_sources[0].amount_debited, D('0.00'))
        self.assertEqual(payment_sources[0].amount_refunded, D('0.00'))

        transactions = payment_sources[0].transactions.all()
        self.assertEqual(transactions.count(), 1)
        self.assertEqual(transactions[0].txn_type, 'Authorise')
        self.assertEqual(transactions[0].amount, order.total_incl_tax)
        self.assertEqual(transactions[0].status, 'ACCEPT')

        self.assertEqual(transactions[0].log_field('req_reference_number'), order.number)
        self.assertEqual(transactions[0].token.card_last4, '1111')

        self.assertEqual(len(mail.outbox), 1)




class CheckoutIntegrationTest(BaseCheckoutTest):
    """Full Integration Test of Checkout"""

    def test_checkout_process(self):
        """Full checkout process using minimal api calls"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)

        cs_url, cs_data = self.do_sign_auth_request(basket_id)

        res = self.do_cybersource_post(cs_url, cs_data)
        self.assertEqual(res.status_code, 302)
        self.check_finished_order(cs_data['reference_number'], product.id)


    def test_add_product_during_auth(self):
        """Test attempting to add a product during the authorize flow"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        # Adding a product here should succeed
        res = self.do_add_to_basket(product.id)
        basket1 = res.data['id']
        self.assertEqual(res.status_code, 200)

        cs_url, cs_data = self.do_sign_auth_request(basket_id)

        # Adding a product here should go to a new basket, not the one we're auth'ing
        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)
        basket2 = res.data['id']
        self.assertNotEqual(basket1, basket2)

        res = self.do_cybersource_post(cs_url, cs_data)
        self.assertEqual(res.status_code, 302)
        self.check_finished_order(cs_data['reference_number'], product.id)

        # Adding a product here should go to basket2, not basket1
        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)
        basket3 = res.data['id']
        self.assertEqual(basket2, basket3)


    def test_pay_for_nothing(self):
        """Test attempting to pay for an empty basket"""
        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        data = {
            "guest_email": "herp@example.com",
            "basket": reverse('basket-detail', args=[basket_id]),
            "shipping_address": {
                "first_name": "fadsf",
                "last_name": "fad",
                "line1": "234 5th Ave",
                "line4": "Manhattan",
                "postcode": "10001",
                "state": "NY",
                "country": reverse('country-detail', args=['US']),
                "phone_number": "+1 (717) 467-1111",
            }
        }
        url = reverse('cybersource-sign-auth-request')
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, 406)


    def test_manipulate_total_pre_auth(self):
        """Test attempting to manipulate basket price when requesting an auth form"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['total_incl_tax'], '10.00')

        url = reverse('cybersource-sign-auth-request')
        data = {
            "guest_email": "herp@example.com",
            "basket": reverse('basket-detail', args=[basket_id]),
            "total": "2.00", # Try and get $10 of product for only $2
            "shipping_address": {
                "first_name": "fadsf",
                "last_name": "fad",
                "line1": "234 5th Ave",
                "line4": "Manhattan",
                "postcode": "10001",
                "state": "NY",
                "country": reverse('country-detail', args=['US']),
                "phone_number": "+1 (717) 467-1111",
            }
        }
        res = self.client.post(url, data, format='json')
        self.assertEqual(res.status_code, 406)


    def test_manipulate_total_during_auth(self):
        """Test attempting to manipulate basket price when requesting auth from CyberSource"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['total_incl_tax'], '10.00')

        cs_url, cs_data = self.do_sign_auth_request(basket_id)

        cs_data['amount'] = '2.00'
        res = requests.post(cs_url, cs_data)
        self.assertEqual(res.status_code, 403)


    def test_free_product(self):
        """Full checkout process using minimal api calls"""
        product = self.create_product(price=D('0.00'))

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)

        cs_url, cs_data = self.do_sign_auth_request(basket_id)

        self.assertEqual(cs_data['amount'], '0.00')

        res = self.do_cybersource_post(cs_url, cs_data)
        self.assertEqual(res.status_code, 302)
        self.check_finished_order(cs_data['reference_number'], product.id)



class CSReplyViewTest(BaseCheckoutTest):
    """Test the CybersourceReplyView with fixtured requests"""

    def prepare_basket(self):
        """Setup a basket and session like SignAuthorizePaymentFormView would normally"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)

        session = self.client.session
        session[CHECKOUT_BASKET_ID] = basket_id
        session[CHECKOUT_ORDER_NUM] = str(randrange(1000000, 9999999))
        session[CHECKOUT_SHIPPING_CODE] = 'free-shipping'
        session.save()
        return session, basket_id, session[CHECKOUT_ORDER_NUM]


    @patch('cybersource.signals.order_placed.send')
    def test_invalid_signature(self, order_placed):
        """Invalid signature should result in 400 Bad Request"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_declined_reply_data(order_number)
        data = cs_factories.sign_reply_data(data)

        data['signature'] = 'abcdef'

        url = reverse('cybersource-reply')
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(mail.outbox), 0, 'Should not send email')
        self.assertEqual(order_placed.call_count, 0, 'Should not trigger signal')
        self.assertEqual(Order.objects.count(), 0, 'Should not make order')


    @patch('cybersource.signals.order_placed.send')
    def test_invalid_request_type(self, order_placed):
        """Bad request type should result in 400 Bad Request"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_declined_reply_data(order_number)

        data["req_transaction_type"] = "payment",

        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(mail.outbox), 0, 'Should not send email')
        self.assertEqual(order_placed.call_count, 0, 'Should not trigger signal')
        self.assertEqual(Order.objects.count(), 0, 'Should not make order')


    @patch('cybersource.signals.order_placed.send')
    def test_duplicate_transaction_id(self, order_placed):
        """Duplicate Transaction ID should result in redirect to the success page"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_accepted_reply_data(order_number)
        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')
        self.assertEqual(order_placed.call_count, 0)
        self.assertEqual(Order.objects.count(), 0)

        resp = self.client.post(url, data)
        self.assertRedirects(resp, reverse('checkout:thank-you'))
        self.assertEqual(order_placed.call_count, 1)
        self.assertEqual(Order.objects.count(), 1)

        resp = self.client.post(url, data)
        self.assertRedirects(resp, reverse('checkout:thank-you'))
        self.assertEqual(order_placed.call_count, 1)
        self.assertEqual(Order.objects.count(), 1)


    @patch('cybersource.signals.order_placed.send')
    def test_invalid_reference_number(self, order_placed):
        """Mismatched reference number should result in 400 Bad Request"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_accepted_reply_data(order_number + 'ABC')
        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(order_placed.call_count, 0)
        self.assertEqual(Order.objects.count(), 0)


    @patch('cybersource.signals.order_placed.send')
    def test_missing_basket(self, order_placed):
        """Missing basket should result in 400 Bad Request"""
        session, basket_id, order_number = self.prepare_basket()
        del session[CHECKOUT_BASKET_ID]
        session.save()
        data = cs_factories.build_accepted_reply_data(order_number)
        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')
        resp = self.client.post(url, data)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(order_placed.call_count, 0)
        self.assertEqual(Order.objects.count(), 0)


    @patch('cybersource.signals.order_placed.send')
    def test_declined_card(self, order_placed):
        """Declined card should should result in redirect to failure page"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_declined_reply_data(order_number)
        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')

        resp = self.client.post(url, data)
        self.assertRedirects(resp, reverse('checkout:index'), fetch_redirect_response=False)

        self.assertEqual(len(mail.outbox), 0, 'Should not send email')
        self.assertEqual(order_placed.call_count, 0, 'Should not trigger signal')
        self.assertEqual(Order.objects.count(), 0, 'Should not make order')


    @patch('cybersource.signals.order_placed.send')
    def test_success(self, order_placed):
        """Successful authorization should create an order and redirect to the success page"""
        session, basket_id, order_number = self.prepare_basket()
        data = cs_factories.build_accepted_reply_data(order_number)
        data = cs_factories.sign_reply_data(data)
        url = reverse('cybersource-reply')
        self.assertEqual(order_placed.call_count, 0)
        resp = self.client.post(url, data)

        self.assertRedirects(resp, reverse('checkout:thank-you'))

        self.assertEqual(len(mail.outbox), 1, 'Should send email')
        self.assertEqual(order_placed.call_count, 1, 'Should trigger order_placed signal')

        order = order_placed.call_args[1]['order']
        self.assertEqual(order.status, 'Authorized', 'Should set order status')
        self.assertEqual(order.basket.id, basket_id, 'Should use basket from session')
        self.assertEqual(order.number, order_number, 'Should use order number from CS request')

        session = self.client.session
        self.assertEquals(session[CHECKOUT_ORDER_ID], order.id, 'Should save order_id in session')

        self.assertEqual(order.sources.count(), 1, 'Should save pPaymentSource')
        source = order.sources.first()
        self.assertEqual(source.currency, 'USD')
        self.assertEqual(source.amount_allocated, D('99.99'))
        self.assertEqual(source.amount_refunded, D('0.00'))
        self.assertEqual(source.amount_debited, D('0.00'))

        self.assertEqual(source.transactions.count(), 1, 'Should save Transaction')
        transaction = source.transactions.first()
        self.assertEqual(transaction.log.data, data)
        self.assertEqual(transaction.token.log, transaction.log)
        self.assertEqual(transaction.token.masked_card_number, 'xxxxxxxxxxxx1111')
        self.assertEqual(transaction.token.card_type, '001')
        self.assertEqual(transaction.txn_type, 'Authorise')
        self.assertEqual(transaction.amount, D('99.99'))
        self.assertEqual(transaction.reference, data['transaction_id'])
        self.assertEqual(transaction.status, 'ACCEPT')
        self.assertEqual(transaction.request_token, data['request_token'])

        self.assertEqual(order.payment_events.count(), 1, 'Should save PaymentEvent')
        event = order.payment_events.first()
        self.assertEqual(event.amount, D('99.99'))
        self.assertEqual(event.reference, data['transaction_id'])
        self.assertEqual(event.event_type.name, 'Authorise')

        self.assertEqual(event.line_quantities.count(), 1, 'Should save PaymentEventQuantity')
        lq = event.line_quantities.first()
        self.assertEqual(lq.line, order.lines.first())
        self.assertEqual(lq.quantity, 1)



class AuthPaymentFormViewTest(BaseCheckoutTest):
    """Test the SignAuthorizePaymentFormView"""

    def prepare_basket(self):
        """Setup a basket so that we can pay for it"""
        product = self.create_product()

        res = self.do_get_basket()
        self.assertEqual(res.status_code, 200)
        basket_id = res.data['id']

        res = self.do_add_to_basket(product.id)
        self.assertEqual(res.status_code, 200)

        return basket_id


    @patch('cybersource.signals.pre_build_auth_request.send')
    @patch('cybersource.signals.pre_calculate_auth_total.send')
    def test_request_auth_form_success(self, pre_calculate_auth_total, pre_build_auth_request):
        basket_id = self.prepare_basket()

        # Add some taxes to the basket
        def add_taxes(sender, basket, shipping_address, **kwargs):
            for line in basket.all_lines():
                line.purchase_info.price.tax = D('0.42')
        pre_calculate_auth_total.side_effect = add_taxes

        # Add an extra field into the request
        def add_a_field(sender, extra_fields, request, basket, **kwargs):
            extra_fields['my_custom_field'] = 'ABC'
        pre_build_auth_request.side_effect = add_a_field

        # Pregenerate the order number
        session = self.client.session
        session[CHECKOUT_ORDER_NUM] = '10000042'
        session.save()

        cs_url, data = self.do_sign_auth_request(basket_id=basket_id)

        # CS URL should be correct
        self.assertEqual(cs_url, 'https://testsecureacceptance.cybersource.com/silent/pay')

        # Basket ID should be stored in the session
        session = self.client.session
        self.assertEqual(session[CHECKOUT_BASKET_ID], basket_id)

        # Basket must be frozen
        basket = Basket.objects.get(id=basket_id)
        self.assertFalse(basket.can_be_edited)

        # Make sure each signal got called
        self.assertEqual(pre_calculate_auth_total.call_count, 1)
        self.assertEqual(pre_build_auth_request.call_count, 1)

        # Check response fields
        self.assertEquals(data['amount'], '10.42')
        self.assertEquals(data['bill_to_address_city'], 'Manhattan')
        self.assertEquals(data['bill_to_address_country'], 'US')
        self.assertEquals(data['bill_to_address_line1'], '234 5th Ave')
        self.assertEquals(data['bill_to_address_line2'], 'apt 5')
        self.assertEquals(data['bill_to_address_postal_code'], '10001')
        self.assertEquals(data['bill_to_address_state'], 'NY')
        self.assertEquals(data['bill_to_email'], 'herp@example.com')
        self.assertEquals(data['bill_to_forename'], 'Testy')
        self.assertEquals(data['bill_to_phone'], '17174671111')
        self.assertEquals(data['bill_to_surname'], 'McUnitTest')
        self.assertEquals(data['card_cvn'], '123')
        self.assertEquals(data['card_expiry_date'], '12-2017')
        self.assertEquals(data['card_number'], '4111111111111111')
        self.assertEquals(data['card_type'], '001')
        self.assertEquals(data['currency'], 'USD')
        self.assertEquals(data['customer_ip_address'], '127.0.0.1')
        self.assertEquals(data['device_fingerprint_id'], '')
        self.assertEquals(data['item_0_name'], 'My Product')
        self.assertEquals(data['item_0_quantity'], '1')
        self.assertEquals(data['item_0_sku'], basket.all_lines()[0].stockrecord.partner_sku)
        self.assertEquals(data['item_0_unit_price'], '10.42')
        self.assertEquals(data['line_item_count'], '1')
        self.assertEquals(data['locale'], 'en')
        self.assertEquals(data['my_custom_field'], 'ABC')
        self.assertEquals(data['payment_method'], 'card')
        self.assertEquals(data['reference_number'], '10000042')
        self.assertEquals(data['ship_to_address_city'], 'Manhattan')
        self.assertEquals(data['ship_to_address_country'], 'US')
        self.assertEquals(data['ship_to_address_line1'], '234 5th Ave')
        self.assertEquals(data['ship_to_address_line2'], '')
        self.assertEquals(data['ship_to_address_postal_code'], '10001')
        self.assertEquals(data['ship_to_address_state'], 'NY')
        self.assertEquals(data['ship_to_forename'], 'fadsf')
        self.assertEquals(data['ship_to_phone'], '17174671111')
        self.assertEquals(data['ship_to_surname'], 'fad')
        self.assertEquals(data['transaction_type'], 'authorization,create_payment_token')
