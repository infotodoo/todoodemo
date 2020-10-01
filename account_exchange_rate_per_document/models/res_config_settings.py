# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'


    exchange_rate_per_document = fields.Boolean('Currency Exchange Rate Per Document',help="Applies to purchase and sale invoices as well as payments")
