"""Gmail data access and message parsing.

Deliberately *not* re-exported here: `epc.gmail.mime` pulls in BeautifulSoup and
lxml, which live in the `classify` extra. The apply worker installs core only and
must never import this subpackage's parser.
"""
