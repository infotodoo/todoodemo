# -*- coding: utf-8 -*-

{
    'name' : 'Factura de compra con tercero en las lineas',
    'version' : '13.0.1.0.1',
    'sequence': 201,
    'category': 'Accounting/Accounting',
    'website' : '',
    'summary' : 'Módulo con ajustes a account_move_line',
    'description' : """
Contabilidad

""",
    'depends': [
        'base',
        'account',
    ],
    'data': [
        'views/account_move.xml',
    ],
    
    'images': [
        "static/description/logo.jpg"
    ],

    'installable': True,
    'application': True,
    'auto_install': False,
}
