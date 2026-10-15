"""Six authored, higher-complexity pinned dependency tasks for preregistration v2.

Checks and references are host-only. No model pilot is used to select tasks.
"""

from textwrap import dedent


def src(text):
    return dedent(text).lstrip()


HARD_FIXTURES = {
    "T01": {
        "package": "httpx", "version": "0.28.1", "blind": True,
        "source_url": "https://www.python-httpx.org/async/#streaming-responses",
        "objective": (
            "Fix async read_events(transport, url, limit) for HTTPX 0.28.1. The caller owns the supplied "
            "AsyncBaseTransport: never close it. Accept only nonnegative integer limits (bool is invalid), "
            "raising ValueError otherwise. Zero returns [] without making a request. Stream GET through "
            "the supplied transport, raise HTTPStatusError for non-2xx responses, transparently decode gzip "
            "and UTF-8 across arbitrary byte chunks, skip blank NDJSON lines, and return at most limit JSON "
            "values in order. Stop parsing at the limit even if later lines are malformed. Propagate JSON "
            "errors before the limit. Close each response stream exactly once on success or any error. "
            "Do not replace the supplied transport, buffer the whole body, or add dependencies."
        ),
        "broken": src('''
            import json
            import httpx

            async def read_events(transport, url, limit):
                async with httpx.AsyncClient(transport=transport) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    return [json.loads(line) for line in response.text.splitlines() if line.strip()][:limit]
        '''),
        "reference": src('''
            import json
            import httpx

            class Borrowed(httpx.AsyncBaseTransport):
                def __init__(self, transport):
                    self.transport = transport
                async def handle_async_request(self, request):
                    return await self.transport.handle_async_request(request)

            async def read_events(transport, url, limit):
                if type(limit) is not int or limit < 0:
                    raise ValueError("invalid limit")
                if limit == 0:
                    return []
                values = []
                async with httpx.AsyncClient(transport=Borrowed(transport)) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        response.encoding = "utf-8"
                        async for line in response.aiter_lines():
                            if line.strip():
                                values.append(json.loads(line))
                                if len(values) == limit:
                                    break
                return values
        '''),
        "checks": src('''
            import asyncio, gzip, json, unittest
            import httpx
            from integration import read_events

            class Stream(httpx.AsyncByteStream):
                def __init__(self, chunks):
                    self.chunks, self.closed, self.reads = chunks, 0, 0
                async def __aiter__(self):
                    for chunk in self.chunks:
                        self.reads += 1
                        yield chunk
                async def aclose(self):
                    self.closed += 1

            class Transport(httpx.AsyncBaseTransport):
                def __init__(self, chunks, status=200, headers=None):
                    self.stream, self.status, self.headers = Stream(chunks), status, headers or {}
                    self.calls, self.closed = 0, 0
                async def handle_async_request(self, request):
                    self.calls += 1
                    return httpx.Response(self.status, headers=self.headers, stream=self.stream)
                async def aclose(self):
                    self.closed += 1

            class Behavior(unittest.TestCase):
                def call(self, transport, limit):
                    return asyncio.run(read_events(transport, "https://local.invalid/events", limit))
                def test_chunked_gzip_unicode_and_blank_lines(self):
                    body = gzip.compress(('\\n{"name":"雪"}\\n\\n{"id":2}\\n').encode())
                    t = Transport([body[i:i+3] for i in range(0, len(body), 3)], headers={"content-encoding":"gzip"})
                    self.assertEqual(self.call(t, 5), [{"name":"雪"}, {"id":2}])
                    self.assertEqual((t.stream.closed, t.closed), (1, 0))
                def test_limit_stops_before_malformed_tail(self):
                    t = Transport([b'{"n":1}\\n', b'not json\\n'])
                    self.assertEqual(self.call(t, 1), [{"n":1}])
                    self.assertEqual(t.stream.reads, 1)
                    self.assertEqual((t.stream.closed, t.closed), (1, 0))
                def test_json_error_closes_only_response(self):
                    t = Transport([b'bad\\n'])
                    with self.assertRaises(json.JSONDecodeError): self.call(t, 2)
                    self.assertEqual((t.stream.closed, t.closed), (1, 0))
                def test_http_error_closes_only_response(self):
                    t = Transport([b'error'], status=503)
                    with self.assertRaises(httpx.HTTPStatusError): self.call(t, 1)
                    self.assertEqual((t.stream.closed, t.closed), (1, 0))
                def test_zero_and_invalid_limits_make_no_request(self):
                    t = Transport([])
                    self.assertEqual(self.call(t, 0), [])
                    for limit in (-1, True, 1.5, "2"):
                        with self.assertRaises(ValueError): self.call(t, limit)
                    self.assertEqual((t.calls, t.closed), (0, 0))
                def test_uncompressed_final_line_without_newline(self):
                    t = Transport([b'  \\n{"n":', b'3}'])
                    self.assertEqual(self.call(t, 9), [{"n":3}])
                    self.assertEqual((t.stream.closed, t.closed), (1, 0))
            if __name__ == "__main__": unittest.main()
        '''),
        "reads": [{"path": "httpx/_models.py", "start_line": 974, "end_line": 1076},
                  {"path": "httpx/_client.py", "start_line": 1542, "end_line": 1592}],
    },
    "T02": {
        "package": "httpx", "version": "0.28.1", "blind": True,
        "source_url": "https://www.python-httpx.org/advanced/transports/",
        "objective": (
            "Implement RetryTransport(inner, max_attempts=3) as an HTTPX 0.28.1 BaseTransport wrapper. "
            "max_attempts is the total attempt cap and must be a positive int, excluding bool; invalid values "
            "raise ValueError. Retry only GET/HEAD/OPTIONS requests whose already-buffered content is empty. "
            "Do not consume an unread request stream to decide eligibility. Retry only ConnectError, "
            "ConnectTimeout, and HTTP 503. Never retry ReadTimeout, other status codes, nonempty bodies, "
            "unsafe methods, or unread streams. Pass the same unmodified request object on every attempt. "
            "Close each discarded 503 before another attempt. Return the final response unclosed and unread, "
            "including a final 503; propagate the final connection exception. No sleeps or real network. "
            "close() must close inner exactly once even if called repeatedly."
        ),
        "broken": src('''
            import httpx
            class RetryTransport(httpx.BaseTransport):
                def __init__(self, inner, max_attempts=3):
                    self.inner, self.max_attempts = inner, max_attempts
                def handle_request(self, request):
                    for _ in range(self.max_attempts):
                        response = self.inner.handle_request(request)
                        if response.status_code != 503:
                            return response
                    return response
                def close(self):
                    self.inner.close()
        '''),
        "reference": src('''
            import httpx
            class RetryTransport(httpx.BaseTransport):
                def __init__(self, inner, max_attempts=3):
                    if type(max_attempts) is not int or max_attempts < 1:
                        raise ValueError("invalid attempt cap")
                    self.inner, self.max_attempts, self.closed = inner, max_attempts, False
                def handle_request(self, request):
                    try:
                        eligible = request.method in ("GET", "HEAD", "OPTIONS") and request.content == b""
                    except httpx.RequestNotRead:
                        eligible = False
                    attempts = self.max_attempts if eligible else 1
                    for attempt in range(attempts):
                        try:
                            response = self.inner.handle_request(request)
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            if attempt + 1 == attempts:
                                raise
                            continue
                        if response.status_code != 503 or attempt + 1 == attempts:
                            return response
                        response.close()
                def close(self):
                    if not self.closed:
                        self.closed = True
                        self.inner.close()
        '''),
        "checks": src('''
            import unittest
            import httpx
            from integration import RetryTransport
            class Stream(httpx.SyncByteStream):
                def __init__(self): self.closed, self.reads = 0, 0
                def __iter__(self):
                    self.reads += 1
                    yield b'data'
                def close(self): self.closed += 1
            class Script(httpx.BaseTransport):
                def __init__(self, actions): self.actions, self.seen, self.closed = list(actions), [], 0
                def handle_request(self, request):
                    self.seen.append(request)
                    action = self.actions.pop(0)
                    if isinstance(action, Exception): raise action
                    return action
                def close(self): self.closed += 1
            def response(status): return httpx.Response(status, stream=Stream())
            class Behavior(unittest.TestCase):
                def test_retry_and_discard_lifecycle(self):
                    a, b = response(503), response(200)
                    inner = Script([httpx.ConnectError('x'), a, b]); wrapper = RetryTransport(inner)
                    req = httpx.Request('GET', 'https://local.invalid/x', headers={'X-Test':'v'})
                    self.assertIs(wrapper.handle_request(req), b)
                    self.assertTrue(all(r is req for r in inner.seen))
                    self.assertEqual((a.stream.closed, a.stream.reads, b.stream.closed, b.stream.reads), (1, 0, 0, 0))
                    wrapper.close(); wrapper.close(); self.assertEqual(inner.closed, 1)
                def test_final_503_remains_owned_by_caller(self):
                    a, b = response(503), response(503); inner = Script([a, b])
                    self.assertIs(RetryTransport(inner, 2).handle_request(httpx.Request('HEAD','https://x.invalid')), b)
                    self.assertEqual((a.stream.closed, b.stream.closed), (1, 0))
                def test_unsafe_or_nonempty_or_unread_requests_not_retried(self):
                    unread = Stream()
                    requests = [httpx.Request('POST','https://x.invalid'), httpx.Request('GET','https://x.invalid',content=b'x'),
                                httpx.Request('GET','https://x.invalid',stream=unread)]
                    for req in requests:
                        a = response(503); inner = Script([a, response(200)])
                        self.assertIs(RetryTransport(inner).handle_request(req), a)
                        self.assertEqual(len(inner.seen), 1); self.assertEqual(a.stream.closed, 0)
                    self.assertEqual(unread.reads, 0)
                def test_read_timeout_and_other_status_not_retried(self):
                    exc = httpx.ReadTimeout('read'); inner = Script([exc, response(200)])
                    with self.assertRaises(httpx.ReadTimeout): RetryTransport(inner).handle_request(httpx.Request('GET','https://x.invalid'))
                    self.assertEqual(len(inner.seen), 1)
                    a = response(429); inner = Script([a, response(200)])
                    self.assertIs(RetryTransport(inner).handle_request(httpx.Request('OPTIONS','https://x.invalid')), a)
                    self.assertEqual(len(inner.seen), 1)
                def test_connection_exhaustion_and_single_attempt(self):
                    final = httpx.ConnectTimeout('last'); inner = Script([httpx.ConnectError('first'), final])
                    with self.assertRaises(httpx.ConnectTimeout) as caught:
                        RetryTransport(inner, 2).handle_request(httpx.Request('GET','https://x.invalid'))
                    self.assertIs(caught.exception, final)
                    inner = Script([httpx.ConnectError('once'), response(200)])
                    with self.assertRaises(httpx.ConnectError): RetryTransport(inner, 1).handle_request(httpx.Request('GET','https://x.invalid'))
                    self.assertEqual(len(inner.seen), 1)
                def test_invalid_attempt_cap(self):
                    for value in (0, -1, True, 1.5, '3'):
                        with self.assertRaises(ValueError): RetryTransport(Script([]), value)
            if __name__ == '__main__': unittest.main()
        '''),
        "reads": [{"path": "httpx/_transports/base.py", "start_line": 1, "end_line": 86},
                  {"path": "httpx/_models.py", "start_line": 942, "end_line": 972}],
    },
    "T03": {
        "package": "pydantic", "version": "2.10.6", "blind": True,
        "source_url": "https://docs.pydantic.dev/2.10/concepts/validators/#validation-context",
        "objective": (
            "Implement parse_batch(payload, cap='100.00') and dump_batch(events) with Pydantic 2.10.6. "
            "parse_batch accepts either a Python list or UTF-8 JSON str/bytes and returns Pydantic models "
            "for a kind-discriminated debit/credit union. Each event requires kind, accountId (or account_id), "
            "amount, currency. account ID is a strict positive int. amount is a finite positive Decimal "
            "with at most two decimal places and cannot exceed this call's cap; cap must not leak between "
            "calls. currency is USD/EUR. Debit requires reason of at least 3 characters; credit permits an "
            "optional reference string. Forbid all other fields and unknown kinds. Invalid data must raise "
            "Pydantic ValidationError with per-item locations. dump_batch emits JSON-compatible dicts with "
            "accountId aliases, Decimal amount/signed values as strings preserving decimal precision, and "
            "omits None. signed is computed as negative amount for debit and positive amount for credit. "
            "Accept empty batches. Use supported v2 APIs and preserve public function names."
        ),
        "broken": src('''
            from decimal import Decimal
            from pydantic import BaseModel
            class Event(BaseModel):
                kind: str
                account_id: int
                amount: Decimal
                currency: str
            def parse_batch(payload, cap='100.00'):
                return [Event.model_validate(row) for row in payload]
            def dump_batch(events):
                return [event.model_dump() for event in events]
        '''),
        "reference": src('''
            from decimal import Decimal
            from typing import Annotated, Literal, Union
            from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, computed_field, field_validator
            class Event(BaseModel):
                model_config = ConfigDict(populate_by_name=True, extra='forbid')
                account_id: Annotated[int, Field(strict=True, gt=0, alias='accountId')]
                amount: Annotated[Decimal, Field(gt=0, decimal_places=2, allow_inf_nan=False)]
                currency: Literal['USD', 'EUR']
                @field_validator('amount')
                @classmethod
                def cap_amount(cls, value, info):
                    if value > info.context['cap']: raise ValueError('over cap')
                    return value
                @computed_field
                @property
                def signed(self) -> Decimal:
                    return -self.amount if self.kind == 'debit' else self.amount
            class Debit(Event):
                kind: Literal['debit']
                reason: Annotated[str, Field(min_length=3)]
            class Credit(Event):
                kind: Literal['credit']
                reference: str | None = None
            adapter = TypeAdapter(list[Annotated[Union[Debit, Credit], Field(discriminator='kind')]])
            def parse_batch(payload, cap='100.00'):
                method = adapter.validate_json if isinstance(payload, (str, bytes)) else adapter.validate_python
                return method(payload, context={'cap':Decimal(str(cap))})
            def dump_batch(events):
                return adapter.dump_python(events, mode='json', by_alias=True, exclude_none=True)
        '''),
        "checks": src('''
            import json, unittest
            from pydantic import BaseModel, ValidationError
            from integration import parse_batch, dump_batch
            def credit(**kw): return {'kind':'credit','accountId':1,'amount':'2.50','currency':'USD',**kw}
            class Behavior(unittest.TestCase):
                def test_python_json_and_alias_serialization(self):
                    rows = [credit(), {'kind':'debit','account_id':2,'amount':'1.20','currency':'EUR','reason':'fee'}]
                    for payload in (rows, json.dumps(rows), json.dumps(rows).encode()):
                        events = parse_batch(payload)
                        self.assertTrue(all(isinstance(e, BaseModel) for e in events))
                        self.assertEqual(dump_batch(events), [credit(signed='2.50'),
                            {'kind':'debit','accountId':2,'amount':'1.20','currency':'EUR','reason':'fee','signed':'-1.20'}])
                        json.dumps(dump_batch(events))
                def test_context_is_per_call(self):
                    self.assertEqual(len(parse_batch([credit(amount='8.00')], cap='9')), 1)
                    with self.assertRaises(ValidationError): parse_batch([credit(amount='8.00')], cap='7')
                    self.assertEqual(len(parse_batch([credit(amount='8.00')], cap='9')), 1)
                def test_strict_id_decimal_and_extra_fields(self):
                    for changes in ({'accountId':True},{'accountId':'1'},{'accountId':0},{'amount':'1.001'},
                                    {'amount':'NaN'},{'amount':'0'},{'amount':'-1'},{'currency':'GBP'},{'admin':True}):
                        with self.subTest(changes=changes), self.assertRaises(ValidationError): parse_batch([credit(**changes)])
                def test_discriminator_and_required_subtype_fields(self):
                    for row in (credit(kind='other'), credit(kind='debit'), credit(kind='debit',reason='x')):
                        with self.assertRaises(ValidationError): parse_batch([row])
                def test_item_error_location(self):
                    with self.assertRaises(ValidationError) as caught: parse_batch([credit(), credit(amount='bad')])
                    self.assertTrue(any(e['loc'][0]==1 and e['loc'][-1]=='amount' for e in caught.exception.errors()))
                def test_optional_reference_and_empty(self):
                    self.assertEqual(dump_batch(parse_batch([credit(reference='abc')])), [credit(reference='abc',signed='2.50')])
                    for payload in ([], '[]', b'[]'): self.assertEqual(dump_batch(parse_batch(payload)), [])
            if __name__ == '__main__': unittest.main()
        '''),
        "reads": [{"path": "pydantic/type_adapter.py", "start_line": 382, "end_line": 448},
                  {"path": "pydantic/type_adapter.py", "start_line": 490, "end_line": 540}],
    },
    "T04": {
        "package": "pydantic_core", "version": "2.27.2", "blind": True,
        "source_url": "https://docs.pydantic.dev/2.10/concepts/types/#handling-third-party-types",
        "objective": (
            "Migrate the custom Money type to Pydantic 2.10.6/pydantic-core 2.27.2. Keep Money(amount, "
            "currency), its amount/currency attributes and make_adapter(). The adapter must accept an existing "
            "Money instance unchanged in Python mode, or a mapping with exactly amount and currency, in both "
            "Python and JSON mode. amount must be a strict string in canonical signed decimal form with exactly "
            "two fractional digits, no leading zeros except zero itself, no exponent or whitespace; negatives "
            "are allowed. currency must be USD/EUR. Invalid mappings, numbers, booleans, nonfinite values, "
            "extra/missing keys and arbitrary strings must raise Pydantic ValidationError. Store amount as "
            "Decimal without float conversion. Serialize to {'amount': fixed-two-place string, 'currency': "
            "currency} in both dump_python and dump_json. TypeAdapter(list[Money]) must work. Generated JSON "
            "schema must describe the mapping, require both fields, forbid extras, expose a string amount "
            "pattern and enumerate currencies. Use supported core-schema hooks, not a v1 validator workaround."
        ),
        "broken": src('''
            from decimal import Decimal
            from pydantic import TypeAdapter
            class Money:
                def __init__(self, amount, currency): self.amount, self.currency = amount, currency
                @classmethod
                def __get_validators__(cls): yield cls.validate
                @classmethod
                def validate(cls, value): return cls(Decimal(str(value['amount'])), value['currency'])
            def make_adapter(): return TypeAdapter(Money)
        '''),
        "reference": src('''
            from decimal import Decimal
            from pydantic import TypeAdapter
            from pydantic_core import core_schema
            class Money:
                def __init__(self, amount, currency): self.amount, self.currency = amount, currency
                @classmethod
                def __get_pydantic_core_schema__(cls, source, handler):
                    mapping = core_schema.typed_dict_schema({
                        'amount': core_schema.typed_dict_field(core_schema.str_schema(strict=True, pattern=r'^-?(0|[1-9][0-9]*)\\.[0-9]{2}$')),
                        'currency': core_schema.typed_dict_field(core_schema.literal_schema(['USD', 'EUR']))
                    }, extra_behavior='forbid')
                    construct = core_schema.chain_schema([mapping, core_schema.no_info_plain_validator_function(
                        lambda v: cls(Decimal(v['amount']), v['currency']))])
                    return core_schema.json_or_python_schema(
                        json_schema=construct,
                        python_schema=core_schema.union_schema([core_schema.is_instance_schema(cls), construct]),
                        serialization=core_schema.plain_serializer_function_ser_schema(
                            lambda m: {'amount':format(m.amount, '.2f'), 'currency':m.currency}, return_schema=mapping))
                @classmethod
                def __get_pydantic_json_schema__(cls, schema, handler):
                    result = handler(schema)
                    result['additionalProperties'] = False
                    return result
            def make_adapter(): return TypeAdapter(Money)
        '''),
        "checks": src('''
            import json, unittest
            from decimal import Decimal
            from pydantic import TypeAdapter, ValidationError
            from integration import Money, make_adapter
            class Behavior(unittest.TestCase):
                def test_python_instance_identity(self):
                    m = Money(Decimal('3.25'), 'USD')
                    self.assertIs(make_adapter().validate_python(m), m)
                def test_mapping_json_and_exact_precision(self):
                    a = make_adapter(); row = {'amount':'123456789123456789.01','currency':'EUR'}
                    for payload in (row, json.dumps(row), json.dumps(row).encode()):
                        m = a.validate_python(payload) if isinstance(payload, dict) else a.validate_json(payload)
                        self.assertIsInstance(m.amount, Decimal)
                        self.assertEqual(m.amount, Decimal(row['amount']))
                        self.assertEqual(a.dump_python(m), row)
                        self.assertEqual(json.loads(a.dump_json(m)), row)
                def test_negatives_and_zero(self):
                    a = make_adapter()
                    for amount in ('-2.50','0.00','-0.00'):
                        row = {'amount':amount,'currency':'USD'}
                        self.assertEqual(a.dump_python(a.validate_python(row)), row)
                def test_invalid_values(self):
                    a = make_adapter()
                    for amount in (1.2, True, Decimal('1.20'), '1.2','01.20','1e2','NaN','Infinity',' 1.20','+1.20'):
                        with self.subTest(amount=amount), self.assertRaises(ValidationError):
                            a.validate_python({'amount':amount,'currency':'USD'})
                    for row in ({'amount':'1.00'}, {'amount':'1.00','currency':'GBP'},
                                {'amount':'1.00','currency':'USD','x':1}, 'USD 1.00'):
                        with self.assertRaises(ValidationError): a.validate_python(row)
                def test_nested_list(self):
                    a = TypeAdapter(list[Money]); rows = [{'amount':'1.20','currency':'USD'},{'amount':'-3.40','currency':'EUR'}]
                    self.assertEqual(json.loads(a.dump_json(a.validate_json(json.dumps(rows)))), rows)
                def test_json_schema(self):
                    schema = make_adapter().json_schema()
                    self.assertEqual(schema['type'], 'object')
                    self.assertEqual(set(schema['required']), {'amount','currency'})
                    self.assertIs(schema['additionalProperties'], False)
                    self.assertEqual(schema['properties']['amount']['type'], 'string')
                    self.assertIn('pattern', schema['properties']['amount'])
                    self.assertEqual(set(schema['properties']['currency']['enum']), {'USD','EUR'})
            if __name__ == '__main__': unittest.main()
        '''),
        "reads": [{"path": "python/pydantic_core/core_schema.py", "start_line": 2789, "end_line": 2961},
                  {"path": "python/pydantic_core/core_schema.py", "start_line": 275, "end_line": 304}],
    },
    "T05": {
        "package": "SQLAlchemy", "version": "2.0.36", "blind": True,
        "source_url": "https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#using-savepoint",
        "objective": (
            "Fix import_rows(session, records) for SQLAlchemy 2.0.36. Keep Base and Item (id primary key; "
            "name unique and non-null). The caller owns the Session and its outer transaction: never "
            "commit, close or roll it back. Attempt each input row in its own savepoint, flush it there, "
            "and continue after IntegrityError. Return {'accepted': successful IDs in input order, "
            "'rejected': zero-based positions of rows failing integrity}. Do not alter existing rows. "
            "Keep earlier successful rows and the caller's preexisting pending changes when a later row "
            "fails. The Session must remain usable afterward. A later caller rollback must undo every "
            "successful insert in this call. Accept empty input and repeated calls. Do not swallow "
            "non-integrity exceptions or add dependencies."
        ),
        "broken": src('''
            from sqlalchemy import Column, Integer, String
            from sqlalchemy.orm import DeclarativeBase
            class Base(DeclarativeBase): pass
            class Item(Base):
                __tablename__ = 'items'
                id = Column(Integer, primary_key=True)
                name = Column(String, unique=True, nullable=False)
            def import_rows(session, records):
                session.add_all([Item(**row) for row in records])
                session.commit()
                return {'accepted':[row['id'] for row in records], 'rejected':[]}
        '''),
        "reference": src('''
            from sqlalchemy import Column, Integer, String
            from sqlalchemy.orm import DeclarativeBase
            from sqlalchemy.exc import IntegrityError
            class Base(DeclarativeBase): pass
            class Item(Base):
                __tablename__ = 'items'
                id = Column(Integer, primary_key=True)
                name = Column(String, unique=True, nullable=False)
            def import_rows(session, records):
                accepted, rejected = [], []
                for position, row in enumerate(records):
                    try:
                        with session.begin_nested():
                            session.add(Item(**row))
                            session.flush()
                    except IntegrityError:
                        rejected.append(position)
                    else:
                        accepted.append(row['id'])
                return {'accepted':accepted, 'rejected':rejected}
        '''),
        "checks": src('''
            import unittest
            from sqlalchemy import create_engine, event, select
            from sqlalchemy.orm import Session
            from integration import Base, Item, import_rows
            class Behavior(unittest.TestCase):
                def setUp(self):
                    self.engine = create_engine('sqlite://')
                    @event.listens_for(self.engine, 'connect')
                    def connect(dbapi, record): dbapi.isolation_level = None
                    @event.listens_for(self.engine, 'begin')
                    def begin(connection): connection.exec_driver_sql('BEGIN')
                    Base.metadata.create_all(self.engine)
                    self.session = Session(self.engine)
                    self.session.add(Item(id=1,name='existing')); self.session.commit()
                    self.session.begin()
                    self.addCleanup(self.engine.dispose); self.addCleanup(self.session.close)
                def names(self): return list(self.session.execute(select(Item.id,Item.name).order_by(Item.id)))
                def test_partial_success_after_constraints(self):
                    rows = [{'id':2,'name':'two'},{'id':3,'name':'existing'},{'id':4,'name':None},{'id':5,'name':'five'}]
                    self.assertEqual(import_rows(self.session,rows), {'accepted':[2,5],'rejected':[1,2]})
                    self.assertEqual(self.names(),[(1,'existing'),(2,'two'),(5,'five')])
                def test_caller_rollback_reverts_successes(self):
                    import_rows(self.session,[{'id':2,'name':'two'}]); self.session.rollback()
                    self.assertEqual(self.names(),[(1,'existing')])
                def test_pending_outer_changes_survive_bad_row(self):
                    self.session.add(Item(id=7,name='pending'))
                    self.assertEqual(import_rows(self.session,[{'id':2,'name':'existing'},{'id':3,'name':'three'}]),
                                     {'accepted':[3],'rejected':[0]})
                    self.assertEqual(self.names(),[(1,'existing'),(3,'three'),(7,'pending')])
                    self.session.rollback(); self.assertEqual(self.names(),[(1,'existing')])
                def test_no_owned_session_operations(self):
                    def forbidden(*args,**kw): raise AssertionError('caller owns the session')
                    original_close = self.session.close
                    self.session.commit = forbidden; self.session.rollback = forbidden; self.session.close = forbidden
                    try: self.assertEqual(import_rows(self.session,[{'id':2,'name':'two'}]),{'accepted':[2],'rejected':[]})
                    finally: self.session.close = original_close
                def test_empty_repeated_and_duplicate_id(self):
                    self.assertEqual(import_rows(self.session,[]),{'accepted':[],'rejected':[]})
                    import_rows(self.session,[{'id':2,'name':'two'}])
                    self.assertEqual(import_rows(self.session,[{'id':2,'name':'different'},{'id':3,'name':'three'}]),
                                     {'accepted':[3],'rejected':[0]})
                    self.assertEqual(self.names(),[(1,'existing'),(2,'two'),(3,'three')])
                def test_non_integrity_errors_propagate(self):
                    with self.assertRaises(TypeError): import_rows(self.session,[{'id':2,'unknown':'x'}])
            if __name__ == '__main__': unittest.main()
        '''),
        "reads": [{"path": "lib/sqlalchemy/orm/session.py", "start_line": 1945, "end_line": 1969},
                  {"path": "lib/sqlalchemy/orm/session.py", "start_line": 4322, "end_line": 4354}],
    },
    "T06": {
        "package": "SQLAlchemy", "version": "2.0.36", "blind": True,
        "source_url": "https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html#joined-eager-loading",
        "objective": (
            "Implement list_accounts(session, term, limit, offset=0) for SQLAlchemy 2.0.36; keep Base, Account "
            "and Entry models and schema. For nonempty term, select accounts having at least one Entry.title "
            "containing that literal case-sensitive text, escaping SQL LIKE wildcard/escape characters. Empty "
            "term selects all accounts including those without entries. Order accounts by ID, apply offset/limit "
            "to distinct accounts, and include ALL entries for each returned account ordered by entry ID. Return "
            "plain dicts {'id': id, 'name': name, 'entries':[{'id':id,'title':title}, ...]}. For a positive limit "
            "use exactly one SELECT, with no lazy or per-account queries. limit/offset must be nonnegative ints "
            "excluding bool; invalid values raise ValueError before querying. A zero limit returns [] without "
            "a query. The caller owns the Session; do not commit or close it. Keep dependencies pinned."
        ),
        "broken": src('''
            from sqlalchemy import Column, ForeignKey, Integer, String
            from sqlalchemy.orm import DeclarativeBase, relationship
            class Base(DeclarativeBase): pass
            class Account(Base):
                __tablename__ = 'accounts'
                id = Column(Integer, primary_key=True)
                name = Column(String, nullable=False)
                entries = relationship('Entry', order_by='Entry.id')
            class Entry(Base):
                __tablename__ = 'entries'
                id = Column(Integer, primary_key=True)
                account_id = Column(ForeignKey('accounts.id'), nullable=False)
                title = Column(String, nullable=False)
            def list_accounts(session, term, limit, offset=0):
                accounts = session.query(Account).join(Entry).filter(Entry.title.contains(term)).order_by(Account.id).offset(offset).limit(limit).all()
                return [{'id':a.id,'name':a.name,'entries':[{'id':e.id,'title':e.title} for e in a.entries]} for a in accounts]
        '''),
        "reference": src('''
            from sqlalchemy import Column, ForeignKey, Integer, String, select
            from sqlalchemy.orm import DeclarativeBase, relationship, joinedload
            class Base(DeclarativeBase): pass
            class Account(Base):
                __tablename__ = 'accounts'
                id = Column(Integer, primary_key=True)
                name = Column(String, nullable=False)
                entries = relationship('Entry', order_by='Entry.id')
            class Entry(Base):
                __tablename__ = 'entries'
                id = Column(Integer, primary_key=True)
                account_id = Column(ForeignKey('accounts.id'), nullable=False)
                title = Column(String, nullable=False)
            def list_accounts(session, term, limit, offset=0):
                if type(limit) is not int or type(offset) is not int or limit < 0 or offset < 0:
                    raise ValueError('invalid page bounds')
                if limit == 0: return []
                statement = select(Account).options(joinedload(Account.entries)).order_by(Account.id).offset(offset).limit(limit)
                if term:
                    statement = statement.where(Account.entries.any(Entry.title.contains(term, autoescape=True)))
                accounts = session.scalars(statement).unique().all()
                return [{'id':a.id,'name':a.name,'entries':[{'id':e.id,'title':e.title} for e in a.entries]} for a in accounts]
        '''),
        "checks": src('''
            import unittest
            from sqlalchemy import create_engine, event
            from sqlalchemy.orm import Session
            from integration import Base, Account, Entry, list_accounts
            class Behavior(unittest.TestCase):
                def setUp(self):
                    self.engine=create_engine('sqlite://')
                    @event.listens_for(self.engine,'connect')
                    def connect(dbapi,record): dbapi.execute('PRAGMA case_sensitive_like=ON')
                    Base.metadata.create_all(self.engine)
                    with Session(self.engine) as seed:
                        seed.add_all([Account(id=i,name='a'+str(i)) for i in range(1,5)])
                        seed.add_all([Entry(id=1,account_id=1,title='match one'),Entry(id=2,account_id=1,title='match two'),
                                      Entry(id=3,account_id=1,title='other'),Entry(id=4,account_id=2,title='match three'),
                                      Entry(id=5,account_id=3,title='literal %_/ slash'),Entry(id=6,account_id=3,title='MATCH')])
                        seed.commit()
                    self.session=Session(self.engine); self.selects=[]
                    @event.listens_for(self.engine,'before_cursor_execute')
                    def count(conn,cursor,statement,params,context,many):
                        if statement.lstrip().upper().startswith('SELECT'): self.selects.append(statement)
                    self.addCleanup(self.engine.dispose); self.addCleanup(self.session.close)
                def call(self,term,limit,offset=0):
                    self.session.expunge_all(); self.selects.clear()
                    rows=list_accounts(self.session,term,limit,offset)
                    self.assertEqual(len(self.selects),1 if limit else 0)
                    return rows
                def test_distinct_parent_pagination(self):
                    self.assertEqual([r['id'] for r in self.call('match',1,1)],[2])
                def test_all_children_and_plain_dicts(self):
                    rows=self.call('match',2)
                    self.assertEqual([r['id'] for r in rows],[1,2])
                    self.assertEqual(rows[0],{'id':1,'name':'a1','entries':[{'id':1,'title':'match one'},{'id':2,'title':'match two'},{'id':3,'title':'other'}]})
                    self.assertTrue(all(type(r) is dict and all(type(e) is dict for e in r['entries']) for r in rows))
                def test_literal_wildcards_escape_and_case(self):
                    for term in ('%','_','/','%_/'):
                        self.assertEqual([r['id'] for r in self.call(term,9)],[3])
                    self.assertEqual([r['id'] for r in self.call('MATCH',9)],[3])
                def test_empty_term_includes_childless_parent(self):
                    rows=self.call('',9)
                    self.assertEqual([r['id'] for r in rows],[1,2,3,4]); self.assertEqual(rows[-1]['entries'],[])
                def test_invalid_and_zero_bounds(self):
                    self.assertEqual(self.call('',0),[])
                    for limit,offset in ((-1,0),(1,-1),(True,0),(1,False),(1.2,0),(1,'0')):
                        self.selects.clear()
                        with self.assertRaises(ValueError): list_accounts(self.session,'',limit,offset)
                        self.assertEqual(self.selects,[])
                def test_no_matches_or_offset_and_session_ownership(self):
                    self.assertEqual(self.call('absent',9),[]); self.assertEqual(self.call('match',1,9),[])
                    original_close=self.session.close
                    def forbidden(*a,**kw): raise AssertionError('caller owns session')
                    self.session.close=forbidden; self.session.commit=forbidden
                    try: self.assertEqual([r['id'] for r in self.call('match',1)],[1])
                    finally: self.session.close=original_close
            if __name__ == '__main__': unittest.main()
        '''),
        "reads": [{"path": "lib/sqlalchemy/orm/strategy_options.py", "start_line": 241, "end_line": 328},
                  {"path": "lib/sqlalchemy/engine/result.py", "start_line": 1036, "end_line": 1086}],
    },
}
