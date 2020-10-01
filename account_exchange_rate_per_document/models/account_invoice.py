# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import RedirectWarning, UserError, ValidationError, AccessError
from odoo.tools import float_is_zero, float_compare, safe_eval, date_utils, email_split, email_escape_char, email_re
from odoo.tools.misc import formatLang, format_date, get_lang

from datetime import date, timedelta
from itertools import groupby
from itertools import zip_longest
from hashlib import sha256
from json import dumps

import json
import re
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    invoice_has_exchange_rate = fields.Boolean('Invoice has currency exchange rate')
    invoice_exchange_rate = fields.Float('Currency Exchange Rate Value', default=1)


    def _compute_base_line_taxes(base_line):

        res = super(AccountMove, self)._compute_base_line_taxes()
        move = base_line.move_id
        if base_line.invoice_has_exchange_rate and self.invoice_exchange_rate > 1:
            if move.is_invoice(include_receipts=True):
                handle_price_include = True
                sign = -1 if move.is_inbound() else 1
                quantity = base_line.quantity
                if base_line.currency_id:
                    price_unit_foreign_curr = sign * base_line.price_unit * (1 - (base_line.discount / 100.0))
                    price_unit_comp_curr = base_line.currency_id._convert_per_document(price_unit_foreign_curr, move.company_id.currency_id, move.company_id, move.date, self.invoice_exchange_rate)
                else:
                    price_unit_foreign_curr = 0.0
                    price_unit_comp_curr = sign * base_line.price_unit * (1 - (base_line.discount / 100.0))
                tax_type = 'sale' if move.type.startswith('out_') else 'purchase'
                is_refund = move.type in ('out_refund', 'in_refund')
            else:
                handle_price_include = False
                quantity = 1.0
                price_unit_foreign_curr = base_line.amount_currency
                price_unit_comp_curr = base_line.balance
                tax_type = base_line.tax_ids[0].type_tax_use if base_line.tax_ids else None
                is_refund = (tax_type == 'sale' and base_line.debit) or (tax_type == 'purchase' and base_line.credit)

            balance_taxes_res = base_line.tax_ids._origin.compute_all(
                price_unit_comp_curr,
                currency=base_line.company_currency_id,
                quantity=quantity,
                product=base_line.product_id,
                partner=base_line.partner_id,
                is_refund=is_refund,
                handle_price_include=handle_price_include,
            )

            if move.type == 'entry':
                repartition_field = is_refund and 'refund_repartition_line_ids' or 'invoice_repartition_line_ids'
                repartition_tags = base_line.tax_ids.mapped(repartition_field).filtered(lambda x: x.repartition_type == 'base').tag_ids
                tags_need_inversion = (tax_type == 'sale' and not is_refund) or (tax_type == 'purchase' and is_refund)
                if tags_need_inversion:
                    balance_taxes_res['base_tags'] = base_line._revert_signed_tags(repartition_tags).ids
                    for tax_res in balance_taxes_res['taxes']:
                        tax_res['tag_ids'] = base_line._revert_signed_tags(self.env['account.account.tag'].browse(tax_res['tag_ids'])).ids

            if base_line.currency_id:
                # Multi-currencies mode: Taxes are computed both in company's currency / foreign currency.
                amount_currency_taxes_res = base_line.tax_ids._origin.compute_all(
                    price_unit_foreign_curr,
                    currency=base_line.currency_id,
                    quantity=quantity,
                    product=base_line.product_id,
                    partner=base_line.partner_id,
                    is_refund=self.type in ('out_refund', 'in_refund'),
                    handle_price_include=handle_price_include,
                )

                if move.type == 'entry':
                    repartition_field = is_refund and 'refund_repartition_line_ids' or 'invoice_repartition_line_ids'
                    repartition_tags = base_line.tax_ids.mapped(repartition_field).filtered(lambda x: x.repartition_type == 'base').tag_ids
                    tags_need_inversion = (tax_type == 'sale' and not is_refund) or (tax_type == 'purchase' and is_refund)
                    if tags_need_inversion:
                        balance_taxes_res['base_tags'] = base_line._revert_signed_tags(repartition_tags).ids
                        for tax_res in balance_taxes_res['taxes']:
                            tax_res['tag_ids'] = base_line._revert_signed_tags(self.env['account.account.tag'].browse(tax_res['tag_ids'])).ids

                for b_tax_res, ac_tax_res in zip(balance_taxes_res['taxes'], amount_currency_taxes_res['taxes']):
                    tax = self.env['account.tax'].browse(b_tax_res['id'])
                    b_tax_res['amount_currency'] = ac_tax_res['amount']

                    # A tax having a fixed amount must be converted into the company currency when dealing with a
                    # foreign currency.
                    if tax.amount_type == 'fixed':
                        b_tax_res['amount'] = base_line.currency_id._convert_per_document(b_tax_res['amount'], move.company_id.currency_id, move.company_id, move.date, move.invoice_exchange_rate)

            return balance_taxes_res
        else:
            return res


    def _compute_payments_widget_to_reconcile_info(self):
        res = super(AccountMove, self)._compute_payments_widget_to_reconcile_info()
        for move in self:
            if move.invoice_has_exchange_rate and move.invoice_exchange_rate > 1:

                move.invoice_outstanding_credits_debits_widget = json.dumps(False)
                move.invoice_has_outstanding = False

                if move.state != 'posted' or move.invoice_payment_state != 'not_paid' or not move.is_invoice(include_receipts=True):
                    continue
                pay_term_line_ids = move.line_ids.filtered(lambda line: line.account_id.user_type_id.type in ('receivable', 'payable'))

                domain = [('account_id', 'in', pay_term_line_ids.mapped('account_id').ids),
                          '|', ('move_id.state', '=', 'posted'), '&', ('move_id.state', '=', 'draft'), ('journal_id.post_at', '=', 'bank_rec'),
                          ('partner_id', '=', move.commercial_partner_id.id),
                          ('reconciled', '=', False), '|', ('amount_residual', '!=', 0.0),
                          ('amount_residual_currency', '!=', 0.0)]

                if move.is_inbound():
                    domain.extend([('credit', '>', 0), ('debit', '=', 0)])
                    type_payment = _('Outstanding credits')
                else:
                    domain.extend([('credit', '=', 0), ('debit', '>', 0)])
                    type_payment = _('Outstanding debits')
                info = {'title': '', 'outstanding': True, 'content': [], 'move_id': move.id}
                lines = self.env['account.move.line'].search(domain)
                currency_id = move.currency_id
                if len(lines) != 0:
                    for line in lines:
                        # get the outstanding residual value in invoice currency
                        if line.currency_id and line.currency_id == move.currency_id:
                            amount_to_show = abs(line.amount_residual_currency)
                        else:
                            currency = line.company_id.currency_id
                            amount_to_show = currency._convert_per_document(abs(line.amount_residual), move.currency_id, move.company_id,
                                                               line.date or fields.Date.today(), move.invoice_exchange_rate)
                        if float_is_zero(amount_to_show, precision_rounding=move.currency_id.rounding):
                            continue
                        info['content'].append({
                            'journal_name': line.ref or line.move_id.name,
                            'amount': amount_to_show,
                            'currency': currency_id.symbol,
                            'id': line.id,
                            'position': currency_id.position,
                            'digits': [69, move.currency_id.decimal_places],
                            'payment_date': fields.Date.to_string(line.date),
                        })
                    info['title'] = type_payment
                    move.invoice_outstanding_credits_debits_widget = json.dumps(info)
                    move.invoice_has_outstanding = True
        else:
            return res


    def _get_reconciled_info_JSON_values(self):
        res = super(AccountMove, self)._get_reconciled_info_JSON_values()

        if self.invoice_has_exchange_rate and self.invoice_exchange_rate > 1:
            self.ensure_one()
            foreign_currency = self.currency_id if self.currency_id != self.company_id.currency_id else False

            reconciled_vals = []
            pay_term_line_ids = self.line_ids.filtered(lambda line: line.account_id.user_type_id.type in ('receivable', 'payable'))
            partials = pay_term_line_ids.mapped('matched_debit_ids') + pay_term_line_ids.mapped('matched_credit_ids')
            for partial in partials:
                counterpart_lines = partial.debit_move_id + partial.credit_move_id
                counterpart_line = counterpart_lines.filtered(lambda line: line not in self.line_ids)

                if foreign_currency and partial.currency_id == foreign_currency:
                    amount = partial.amount_currency
                else:
                    amount = partial.company_currency_id._convert_per_document(partial.amount, self.currency_id, self.company_id, self.date, self.invoice_exchange_rate)

                if float_is_zero(amount, precision_rounding=self.currency_id.rounding):
                    continue

                ref = counterpart_line.move_id.name
                if counterpart_line.move_id.ref:
                    ref += ' (' + counterpart_line.move_id.ref + ')'

                reconciled_vals.append({
                    'name': counterpart_line.name,
                    'journal_name': counterpart_line.journal_id.name,
                    'amount': amount,
                    'currency': self.currency_id.symbol,
                    'digits': [69, self.currency_id.decimal_places],
                    'position': self.currency_id.position,
                    'date': counterpart_line.date,
                    'payment_id': counterpart_line.id,
                    'account_payment_id': counterpart_line.payment_id.id,
                    'payment_method_name': counterpart_line.payment_id.payment_method_id.name if counterpart_line.journal_id.type == 'bank' else None,
                    'move_id': counterpart_line.move_id.id,
                    'ref': ref,
                })
            return reconciled_vals
        else:
            return res


    def _recompute_cash_rounding_lines(self):
        res = super(AccountMove, self)._recompute_cash_rounding_lines()
        return res

        def _compute_cash_rounding(self, total_balance, total_amount_currency):
            res = super(AccountMove, self)._compute_cash_rounding()
            if self.invoice_has_exchange_rate and self.invoice_exchange_rate > 1:
                if self.currency_id == self.company_id.currency_id:
                    diff_balance = self.invoice_cash_rounding_id.compute_difference(self.currency_id, total_balance)
                    diff_amount_currency = 0.0
                else:
                    diff_amount_currency = self.invoice_cash_rounding_id.compute_difference(self.currency_id, total_amount_currency)
                    if self.invoice_has_exchange_rate:
                        diff_balance = self.currency_id._convert_pear_document(diff_amount_currency, self.company_id.currency_id, self.company_id, self.date, self.invoice_exchange_rate)
                    else:
                        diff_balance = self.currency_id._convert(diff_amount_currency, self.company_id.currency_id, self.company_id, self.date)
                return diff_balance, diff_amount_currency
            else:
                return res


    def _inverse_amount_total(self):
        res = super(AccountMove, self)._inverse_amount_total()
        for move in self:
            if move.invoice_has_exchange_rate and self.invoice_exchange_rate > 1:
                for move in self:
                    if len(move.line_ids) != 2 or move.is_invoice(include_receipts=True):
                        continue

                    to_write = []
                    if move.currency_id != move.company_id.currency_id:
                        amount_currency = abs(move.amount_total)
                        if move.invoice_has_exchange_rate:
                            balance = move.currency_id._convert_per_document(amount_currency, move.company_currency_id, move.company_id, move.date, move.invoice_exchange_rate)
                        else:
                            balance = move.currency_id._convert(amount_currency, move.company_currency_id, move.company_id, move.date)
                    else:
                        balance = abs(move.amount_total)
                        amount_currency = 0.0

                    for line in move.line_ids:
                        if float_compare(abs(line.balance), balance, precision_rounding=move.currency_id.rounding) != 0:
                            to_write.append((1, line.id, {
                                'debit': line.balance > 0.0 and balance or 0.0,
                                'credit': line.balance < 0.0 and balance or 0.0,
                                'amount_currency': line.balance > 0.0 and amount_currency or -amount_currency,
                            }))
                    move.write({'line_ids': to_write})
            else:
                return res




    @api.depends('line_ids.price_subtotal', 'line_ids.tax_base_amount', 'line_ids.tax_line_id', 'partner_id', 'currency_id')
    def _compute_invoice_taxes_by_group(self):

        res = super(AccountMove, self)._compute_invoice_taxes_by_group()
        for move in self:
            if move.invoice_has_exchange_rate and self.invoice_exchange_rate > 1:


                lang_env = move.with_context(lang=move.partner_id.lang).env
                tax_lines = move.line_ids.filtered(lambda line: line.tax_line_id)
                tax_balance_multiplicator = -1 if move.is_inbound(True) else 1
                res = {}
                # There are as many tax line as there are repartition lines
                done_taxes = set()
                for line in tax_lines:
                    res.setdefault(line.tax_line_id.tax_group_id, {'base': 0.0, 'amount': 0.0})
                    res[line.tax_line_id.tax_group_id]['amount'] += tax_balance_multiplicator * (line.amount_currency if line.currency_id else line.balance)
                    tax_key_add_base = tuple(move._get_tax_key_for_group_add_base(line))
                    if tax_key_add_base not in done_taxes:
                        if line.currency_id and line.company_currency_id and line.currency_id != line.company_currency_id:
                            if move.invoice_has_exchange_rate:
                                amount = line.company_currency_id._convert_per_document(line.tax_base_amount, line.currency_id, line.company_id, line.date or fields.Date.today(), move.invoice_exchange_rate)
                            else:
                                amount = line.company_currency_id._convert(line.tax_base_amount, line.currency_id, line.company_id, line.date or fields.Date.today())
                        else:
                            amount = line.tax_base_amount
                        res[line.tax_line_id.tax_group_id]['base'] += amount
                        # The base should be added ONCE
                        done_taxes.add(tax_key_add_base)

                # At this point we only want to keep the taxes with a zero amount since they do not
                # generate a tax line.
                zero_taxes = set()
                for line in move.line_ids:
                    for tax in line.tax_ids.flatten_taxes_hierarchy():
                        if tax.tax_group_id not in res or tax.id in zero_taxes:
                            res.setdefault(tax.tax_group_id, {'base': 0.0, 'amount': 0.0})
                            res[tax.tax_group_id]['base'] += tax_balance_multiplicator * (line.amount_currency if line.currency_id else line.balance)
                            zero_taxes.add(tax.id)

                res = sorted(res.items(), key=lambda l: l[0].sequence)
                move.amount_by_group = [(
                    group.name, amounts['amount'],
                    amounts['base'],
                    formatLang(lang_env, amounts['amount'], currency_obj=move.currency_id),
                    formatLang(lang_env, amounts['base'], currency_obj=move.currency_id),
                    len(res),
                    group.id
                ) for group, amounts in res]



            else:
                return res



class AccountMoveLine(models.Model):
    _inherit = "account.move.line"


    @api.model
    def _get_fields_onchange_subtotal_model(self, price_subtotal, move_type, currency, company, date):
        res = super(AccountMoveLine, self)._get_fields_onchange_subtotal_model(price_subtotal, move_type, currency, company, date)
        if self.move_id.invoice_has_exchange_rate and self.move_id.invoice_exchange_rate > 1:
            if move_type in self.move_id.get_outbound_types():
                sign = 1
            elif move_type in self.move_id.get_inbound_types():
                sign = -1
            else:
                sign = 1
            price_subtotal *= sign

            if currency and currency != company.currency_id:
                # Multi-currencies.
                balance = currency._convert_per_document(price_subtotal, company.currency_id, company, date, self.move_id.invoice_exchange_rate)
                return {
                    'amount_currency': price_subtotal,
                    'debit': balance > 0.0 and balance or 0.0,
                    'credit': balance < 0.0 and -balance or 0.0,
                }
            else:
                # Single-currency.
                return {
                    'amount_currency': 0.0,
                    'debit': price_subtotal > 0.0 and price_subtotal or 0.0,
                    'credit': price_subtotal < 0.0 and -price_subtotal or 0.0,
                }
        else:
            return res


    @api.onchange('product_id')
    def _onchange_product_id(self):
        res = super(AccountMoveLine, self)._onchange_product_id()
        if self.move_id.invoice_has_exchange_rate and self.move_id.invoice_exchange_rate > 1:
            for line in self:
                if not line.product_id or line.display_type in ('line_section', 'line_note'):
                    continue

                line.name = line._get_computed_name()
                line.account_id = line._get_computed_account()
                line.tax_ids = line._get_computed_taxes()
                line.product_uom_id = line._get_computed_uom()
                line.price_unit = line._get_computed_price_unit()

                if line.tax_ids and line.move_id.fiscal_position_id:
                    line.price_unit = line._get_price_total_and_subtotal()['price_subtotal']
                    line.tax_ids = line.move_id.fiscal_position_id.map_tax(line.tax_ids._origin, partner=line.move_id.partner_id)
                    accounting_vals = line._get_fields_onchange_subtotal(price_subtotal=line.price_unit, currency=line.move_id.company_currency_id)
                    balance = accounting_vals['debit'] - accounting_vals['credit']
                    line.price_unit = line._get_fields_onchange_balance(balance=balance).get('price_unit', line.price_unit)

                # Convert the unit price to the invoice's currency.
                company = line.move_id.company_id
                #line.price_unit = company.currency_id._convert_per_document(line.price_unit, line.move_id.currency_id, company, line.move_id.date, self.move_id.invoice_exchange_rate)
                line.price_unit = line.price_unit / self.move_id.invoice_exchange_rate
                line.price_unit = line.price_unit if line.price_unit >= 1 else 1

            if len(self) == 1:
                return {'domain': {'product_uom_id': [('category_id', '=', self.product_uom_id.category_id.id)]}}
        else:
            return res


    @api.onchange('product_uom_id')
    def _onchange_uom_id(self):
        res = super(AccountMoveLine, self)._onchange_uom_id()
        if self.move_id.invoice_has_exchange_rate and self.move_id.invoice_exchange_rate > 1:
            price_unit = self._get_computed_price_unit()

            # See '_onchange_product_id' for details.
            taxes = self._get_computed_taxes()
            if taxes and self.move_id.fiscal_position_id:
                price_subtotal = self._get_price_total_and_subtotal(price_unit=price_unit, taxes=taxes)['price_subtotal']
                accounting_vals = self._get_fields_onchange_subtotal(price_subtotal=price_subtotal, currency=self.move_id.company_currency_id)
                balance = accounting_vals['debit'] - accounting_vals['credit']
                price_unit = self._get_fields_onchange_balance(balance=balance).get('price_unit', price_unit)

            # Convert the unit price to the invoice's currency.
            company = self.move_id.company_id
            self.price_unit = price_unit / self.move_id.invoice_exchange_rate
            self.price_unit = self.price_unit if self.price_unit >= 1 else 1
        else:
            return res


    def _recompute_debit_credit_from_amount_currency(self):
        res = super(AccountMoveLine, self)._recompute_debit_credit_from_amount_currency()
        if self.move_id.invoice_has_exchange_rate and self.move_id.invoice_exchange_rate > 1:
            for line in self:
                # Recompute the debit/credit based on amount_currency/currency_id and date.

                company_currency = line.account_id.company_id.currency_id
                balance = line.amount_currency
                if line.currency_id and company_currency and line.currency_id != company_currency:
                    balance = line.currency_id._convert_per_document(balance, company_currency, line.account_id.company_id, line.move_id.date or fields.Date.today(), self.move_id.invoice_exchange_rate)
                    line.debit = balance > 0 and balance or 0.0
                    line.credit = balance < 0 and -balance or 0.0
        else:
            return res



    def check_full_reconcile(self):
        res = super(AccountMoveLine, self).check_full_reconcile()
        if self.move_id.invoice_has_exchange_rate and self.move_id.invoice_exchange_rate > 1:
            # Get first all aml involved
            todo = self.env['account.partial.reconcile'].search_read(['|', ('debit_move_id', 'in', self.ids), ('credit_move_id', 'in', self.ids)], ['debit_move_id', 'credit_move_id'])
            amls = set(self.ids)
            seen = set()
            while todo:
                aml_ids = [rec['debit_move_id'][0] for rec in todo if rec['debit_move_id']] + [rec['credit_move_id'][0] for rec in todo if rec['credit_move_id']]
                amls |= set(aml_ids)
                seen |= set([rec['id'] for rec in todo])
                todo = self.env['account.partial.reconcile'].search_read(['&', '|', ('credit_move_id', 'in', aml_ids), ('debit_move_id', 'in', aml_ids), '!', ('id', 'in', list(seen))], ['debit_move_id', 'credit_move_id'])

            partial_rec_ids = list(seen)
            if not amls:
                return
            else:
                amls = self.browse(list(amls))

            # If we have multiple currency, we can only base ourselves on debit-credit to see if it is fully reconciled
            currency = set([a.currency_id for a in amls if a.currency_id.id != False])
            multiple_currency = False
            if len(currency) != 1:
                currency = False
                multiple_currency = True
            else:
                currency = list(currency)[0]
            # Get the sum(debit, credit, amount_currency) of all amls involved
            total_debit = 0
            total_credit = 0
            total_amount_currency = 0
            maxdate = date.min
            to_balance = {}
            cash_basis_partial = self.env['account.partial.reconcile']
            for aml in amls:
                cash_basis_partial |= aml.move_id.tax_cash_basis_rec_id
                total_debit += aml.debit
                total_credit += aml.credit
                maxdate = max(aml.date, maxdate)
                total_amount_currency += aml.amount_currency
                # Convert in currency if we only have one currency and no amount_currency
                if not aml.amount_currency and currency:
                    multiple_currency = True
                    total_amount_currency += aml.company_id.currency_id._convert_per_document(aml.balance, currency, aml.company_id, aml.date, aml.move_id.invoice_exchange_rate)
                # If we still have residual value, it means that this move might need to be balanced using an exchange rate entry
                if aml.amount_residual != 0 or aml.amount_residual_currency != 0:
                    if not to_balance.get(aml.currency_id):
                        to_balance[aml.currency_id] = [self.env['account.move.line'], 0]
                    to_balance[aml.currency_id][0] += aml
                    to_balance[aml.currency_id][1] += aml.amount_residual != 0 and aml.amount_residual or aml.amount_residual_currency


            digits_rounding_precision = amls[0].company_id.currency_id.rounding
            caba_reconciled_amls = cash_basis_partial.mapped('debit_move_id') + cash_basis_partial.mapped('credit_move_id')
            caba_connected_amls = amls.filtered(lambda x: x.move_id.tax_cash_basis_rec_id) + caba_reconciled_amls
            matched_percentages = caba_connected_amls._get_matched_percentage()
            if (
                    all(matched_percentages[aml.move_id.id] >= 1.0 for aml in caba_connected_amls)
                    and
                    (
                        currency and float_is_zero(total_amount_currency, precision_rounding=currency.rounding) or
                        multiple_currency and float_compare(total_debit, total_credit, precision_rounding=digits_rounding_precision) == 0
                    )
            ):

                exchange_move_id = False
                missing_exchange_difference = False
                # Eventually create a journal entry to book the difference due to foreign currency's exchange rate that fluctuates
                if to_balance and any([not float_is_zero(residual, precision_rounding=digits_rounding_precision) for aml, residual in to_balance.values()]):
                    if not self.env.context.get('no_exchange_difference'):
                        exchange_move_vals = self.env['account.full.reconcile']._prepare_exchange_diff_move(
                            move_date=maxdate, company=amls[0].company_id)
                        if len(amls.mapped('partner_id')) == 1 and amls[0].partner_id:
                            exchange_move_vals['partner_id'] = amls[0].partner_id.id
                        exchange_move = self.env['account.move'].with_context(default_type='entry').create(exchange_move_vals)
                        part_reconcile = self.env['account.partial.reconcile']
                        for aml_to_balance, total in to_balance.values():
                            if total:
                                rate_diff_amls, rate_diff_partial_rec = part_reconcile.create_exchange_rate_entry(aml_to_balance, exchange_move)
                                amls += rate_diff_amls
                                partial_rec_ids += rate_diff_partial_rec.ids
                            else:
                                aml_to_balance.reconcile()
                        exchange_move.post()
                        exchange_move_id = exchange_move.id
                    else:
                        missing_exchange_difference = True
                if not missing_exchange_difference:
                    #mark the reference of the full reconciliation on the exchange rate entries and on the entries
                    self.env['account.full.reconcile'].create({
                        'partial_reconcile_ids': [(6, 0, partial_rec_ids)],
                        'reconciled_line_ids': [(6, 0, amls.ids)],
                        'exchange_move_id': exchange_move_id,
                    })
        else:
            return res
