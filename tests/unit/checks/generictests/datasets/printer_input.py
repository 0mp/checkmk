# yapf: disable


checkname = 'printer_input'


info = [[u'1.1', u'Printer 1', u'MP Tray', u'19', u'8', u'150', u'0'],
        [u'1.2', u'Custom Printer Name 1', u'Cassette 1', u'4', u'8', u'500', u'400'],
        [u'1.3', u'', u'Cassette 2', u'0', u'8', u'300', u'150']]


discovery = {'': [(u'Cassette 2', {}), (u'Custom Printer Name 1', {})]}


checks = {'': [(u'Cassette 2',
                {'capacity_levels': (0.0, 0.0)},
                [(0, u'Cassette 2', []),
                 (0, 'Status: Available and Idle', []),
                 (0, 'Capacity: 50.00% of 300 sheets remaining', [])]),
               (u'Custom Printer Name 1',
                {'capacity_levels': (0.0, 0.0)},
                [(0, u'Cassette 1', []),
                 (0, 'Status: Available and Active', []),
                 (0, 'Capacity: 80.00% of 500 sheets remaining', [])])]}