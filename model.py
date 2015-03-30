# encoding: utf-8
import operator
import md5
from collections import (
    Counter,
    defaultdict,
)
import bisect
from cStringIO import StringIO
import datetime
import json
import os
from nose.tools import set_trace
import md5
import random
import re
import requests
import time
import isbnlib
import urllib
import traceback

from PIL import (
    Image,
)

from sqlalchemy.engine.url import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import (
    backref,
    relationship,
)
from sqlalchemy import or_
from sqlalchemy.orm import (
    aliased,
    backref,
    joinedload,
)
from sqlalchemy.orm.exc import (
    NoResultFound,
    MultipleResultsFound,
)
from sqlalchemy.ext.mutable import (
    MutableDict,
)
from sqlalchemy.ext.associationproxy import (
    association_proxy,
)
from sqlalchemy.sql.functions import func
from sqlalchemy.sql.expression import (
    and_,
    or_,
)
from sqlalchemy.exc import (
    IntegrityError
)
from sqlalchemy import (
    create_engine, 
    Binary,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Index,
    String,
    Table,
    Unicode,
    UniqueConstraint,
)

import classifier
from classifier import (
    Classifier,
    GenreData,
)
from util import (
    LanguageCodes,
    MetadataSimilarity,
    TitleProcessor,
)
from util.permanent_work_id import WorkIDCalculator
from util.summary import SummaryEvaluator

#import logging
#logging.basicConfig()
#logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

from sqlalchemy.orm.session import Session

from sqlalchemy.dialects.postgresql import (
    ARRAY,
    HSTORE,
    JSON,
)
from sqlalchemy.orm import sessionmaker
from s3 import S3Uploader

DEBUG = False

def production_session():
    url = os.environ['DATABASE_URL']
    print url
    if url.startswith('"'):
        url = url[1:]
    print "ENVIRONMENT: %s" % os.environ['DATABASE_URL'] 
    print "MODIFIED: %s" % url
    return SessionManager.session(url)

class SessionManager(object):

    @classmethod
    def engine(cls, url=None):
        url = url or os.environ['DATABASE_URL']
        return create_engine(url, echo=DEBUG)

    @classmethod
    def initialize(cls, url):
        engine = cls.engine(url)
        Base.metadata.create_all(engine)
        return engine, engine.connect()

    @classmethod
    def session(cls, url):
        engine, connection = cls.initialize(url)
        session = Session(connection)
        print "INITIALIZING DATA"
        cls.initialize_data(session)
        session.commit()
        print "DONE INITIALIZING DATA"
        return session

    @classmethod
    def initialize_data(cls, session):
        # Create initial data sources.
        list(DataSource.well_known_sources(session))

        # Create all genres.
        for g in classifier.genres.values():
            Genre.lookup(session, g, autocreate=True)
        session.commit()

def get_one(db, model, on_multiple='error', **kwargs):
    q = db.query(model).filter_by(**kwargs)
    try:
        return q.one()
    except MultipleResultsFound, e:
        if on_multiple == 'error':
            raise e
        elif on_multiple == 'interchangeable':
            # These records are interchangeable so we can use
            # whichever one we want.
            #
            # This may be a sign of a problem somewhere else. A
            # database-level constraint might be useful.
            q = q.limit(1)
            return q.one()
    except NoResultFound:
        return None


def get_one_or_create(db, model, create_method='',
                      create_method_kwargs=None,
                      **kwargs):
    one = get_one(db, model, **kwargs)
    if one:
        return one, False
    else:
        try:
            if 'on_multiple' in kwargs:
                # This kwarg is supported by get_one() but not by create().
                del kwargs['on_multiple']
            return create(db, model, create_method, create_method_kwargs, **kwargs)
        except IntegrityError:
            db.rollback()
            return db.query(model).filter_by(**kwargs).one(), False

def create(db, model, create_method='',
           create_method_kwargs=None,
           **kwargs):
    kwargs.update(create_method_kwargs or {})
    created = getattr(model, create_method, model)(**kwargs)
    db.add(created)
    db.flush()
    return created, True

Base = declarative_base()

class Patron(Base):

    __tablename__ = 'patrons'
    id = Column(Integer, primary_key=True)

    # The patron's permanent unique identifier in an external library
    # system, probably never seen by the patron.
    external_identifier = Column(Unicode, unique=True, index=True)

    # An identifier used by the patron that gives them the authority
    # to borrow books. This identifier may change over time.
    authorization_identifier = Column(Unicode, unique=True, index=True)

    # TODO: An identifier used by the patron that authenticates them,
    # but does not give them the authority to borrow books. i.e. their
    # website username.

    # The last time this record was synced up with an external library
    # system.
    last_external_sync = Column(DateTime)

    # The time, if any, at which the user's authorization to borrow
    # books expires.
    authorization_expires = Column(Date, index=True)

    loans = relationship('Loan', backref='patron')
    holds = relationship('Hold', backref='patron')

    # One Patron can have many associated Credentials.
    credentials = relationship("Credential", backref="patron")

    def works_on_loan(self):
        db = Session.object_session(self)
        loans = db.query(Loan).filter(Loan.patron==self)
        return [loan.license_pool.work for loan in loans]

    def works_on_loan_or_on_hold(self):
        db = Session.object_session(self)
        results = set()
        holds_q = db.query(Hold).filter(Loan.patron==self)
        holds = [hold.license_pool.work for hold in holds_q]
        loans = self.works_on_loan()
        return set(holds + loans)

    @property
    def authorization_is_active(self):
        # Unlike pretty much every other place in this app, I use
        # (server) local time here instead of UTC. This is to make it
        # less likely that a patron's authorization will expire before
        # they think it should.
        if (self.authorization_expires
            and self.authorization_expires 
            < datetime.datetime.now().date()):
            return False
        return True


class Loan(Base):
    __tablename__ = 'loans'
    id = Column(Integer, primary_key=True)
    patron_id = Column(Integer, ForeignKey('patrons.id'), index=True)
    license_pool_id = Column(Integer, ForeignKey('licensepools.id'), index=True)
    start = Column(DateTime)
    end = Column(DateTime)


class Hold(Base):
    """A patron is in line to check out a book.
    """
    __tablename__ = 'holds'
    id = Column(Integer, primary_key=True)
    patron_id = Column(Integer, ForeignKey('patrons.id'), index=True)
    license_pool_id = Column(Integer, ForeignKey('licensepools.id'), index=True)
    start = Column(DateTime, index=True)
    end = Column(DateTime, index=True)
    position = Column(Integer, index=True)

    def update(self, start, end, position):
        """When the book becomes available, position will be 0 and end will be
        set to the time at which point the patron will lose their place in
        line.
        
        Otherwise, end is irrelevant and is set to None.
        """
        self.start = start
        if position == 0:
            self.end = end
        else:
            self.end = None
        self.position = position


class DataSource(Base):

    """A source for information about books, and possibly the books themselves."""

    GUTENBERG = "Gutenberg"
    OVERDRIVE = "Overdrive"
    THREEM = "3M"
    OCLC = "OCLC Classify"
    OCLC_LINKED_DATA = "OCLC Linked Data"
    AMAZON = "Amazon"
    XID = "WorldCat xID"
    AXIS_360 = "Axis 360"
    WEB = "Web"
    OPEN_LIBRARY = "Open Library"
    CONTENT_CAFE = "Content Cafe"
    VIAF = "Content Cafe"
    GUTENBERG_COVER_GENERATOR = "Gutenberg Illustrated"
    GUTENBERG_EPUB_GENERATOR = "Project Gutenberg EPUB Generator"
    BIBLIOCOMMONS = "BiblioCommons"
    MANUAL = "Manual intervention"
    NYT = "New York Times"
    LIBRARY_STAFF = "Library staff"

    __tablename__ = 'datasources'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    offers_licenses = Column(Boolean, default=False)
    primary_identifier_type = Column(String, index=True)
    extra = Column(MutableDict.as_mutable(JSON), default={})

    # One DataSource can generate many Editions.
    editions = relationship("Edition", backref="data_source")

    # One DataSource can generate many CoverageRecords.
    coverage_records = relationship("CoverageRecord", backref="data_source")

    # One DataSource can generate many IDEquivalencies.
    id_equivalencies = relationship("Equivalency", backref="data_source")

    # One DataSource can grant access to many LicensePools.
    license_pools = relationship(
        "LicensePool", backref=backref("data_source", lazy='joined'))

    # One DataSource can provide many Hyperlinks.
    links = relationship("Hyperlink", backref="data_source")

    # One DataSource can provide many Resources.
    resources = relationship("Resource", backref="data_source")

    # One DataSource can generate many Measurements.
    measurements = relationship("Measurement", backref="data_source")

    # One DataSource can provide many Classifications.
    classifications = relationship("Classification", backref="data_source")

    # One DataSource can have many associated Credentials.
    credentials = relationship("Credential", backref="data_source")

    # One DataSource can generate many CustomLists.
    custom_lists = relationship("CustomList", backref="data_source")

    @classmethod
    def lookup(cls, _db, name):
        try:
            q = _db.query(cls).filter_by(name=name)
            return q.one()
        except NoResultFound:
            return None

    @classmethod
    def license_source_for(cls, _db, identifier):
        """Finds the DataSource that provide licenses for books identified
        by the given identifier.

        If there is no such DataSource, or there is more than one,
        raises an exception.
        """
        if isinstance(identifier, basestring):
            type = identifier
        else:
            type = identifier.type
        q =_db.query(DataSource).filter(DataSource.offers_licenses==True).filter(
            DataSource.primary_identifier_type==type)
        return q.one()

    @classmethod
    def well_known_sources(cls, _db):
        """Make sure all the well-known sources exist."""

        for (name, offers_licenses, primary_identifier_type, refresh_rate) in (
                (cls.GUTENBERG, True, Identifier.GUTENBERG_ID, None),
                (cls.OVERDRIVE, True, Identifier.OVERDRIVE_ID, 0),
                (cls.THREEM, True, Identifier.THREEM_ID, 60*60*6),
                (cls.AXIS_360, True, Identifier.AXIS_360_ID, 0),
                (cls.OCLC, False, Identifier.OCLC_NUMBER, None),
                (cls.OCLC_LINKED_DATA, False, Identifier.OCLC_NUMBER, None),
                (cls.AMAZON, False, Identifier.ASIN, None),
                (cls.OPEN_LIBRARY, False, Identifier.OPEN_LIBRARY_ID, None),
                (cls.GUTENBERG_COVER_GENERATOR, False, Identifier.GUTENBERG_ID, None),
                (cls.GUTENBERG_EPUB_GENERATOR, False, Identifier.GUTENBERG_ID, None),
                (cls.WEB, True, Identifier.URI, None),
                (cls.VIAF, False, None, None),
                (cls.CONTENT_CAFE, False, None, None),
                (cls.BIBLIOCOMMONS, False, Identifier.BIBLIOCOMMONS_ID, None),
                (cls.MANUAL, False, None, None),
                (cls.NYT, False, Identifier.ISBN, None),
                (cls.LIBRARY_STAFF, False, Identifier.ISBN, None),
        ):

            extra = dict()
            if refresh_rate:
                extra['circulation_refresh_rate_seconds'] = refresh_rate

            obj, new = get_one_or_create(
                _db, DataSource,
                name=name,
                create_method_kwargs=dict(
                    offers_licenses=offers_licenses,
                    primary_identifier_type=primary_identifier_type,
                    extra=extra,
                )
            )
            yield obj


class CoverageRecord(Base):
    """A record of a Identifier being used as input into another data
    source.
    """
    __tablename__ = 'coveragerecords'

    id = Column(Integer, primary_key=True)
    identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True)
    data_source_id = Column(
        Integer, ForeignKey('datasources.id'), index=True)
    date = Column(Date, index=True)
    exception = Column(Unicode, index=True)


class Equivalency(Base):
    """An assertion that two Identifiers identify the same work.

    This assertion comes with a 'strength' which represents how confident
    the data source is in the assertion.
    """
    __tablename__ = 'equivalents'

    # 'input' is the ID that was used as input to the datasource.
    # 'output' is the output
    id = Column(Integer, primary_key=True)
    input_id = Column(Integer, ForeignKey('identifiers.id'), index=True)
    input = relationship("Identifier", foreign_keys=input_id)
    output_id = Column(Integer, ForeignKey('identifiers.id'), index=True)
    output = relationship("Identifier", foreign_keys=output_id)

    # Who says?
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)

    # How many distinct votes went into this assertion? This will let
    # us scale the change to the strength when additional votes come
    # in.
    votes = Column(Integer, default=1)

    # How strong is this assertion (-1..1)? A negative number is an
    # assertion that the two Identifiers do *not* identify the
    # same work.
    strength = Column(Float, index=True)

    def __repr__(self):
        r = u"[%s ->\n %s\n source=%s strength=%.2f votes=%d)]" % (
            repr(self.input).decode("utf8"),
            repr(self.output).decode("utf8"),
            self.data_source.name, self.strength, self.votes
        )
        return r.encode("utf8")

    @classmethod
    def for_identifiers(self, _db, identifiers, exclude_ids=None):
        """Find all Equivalencies for the given Identifiers."""
        if not identifiers:
            return []
        if isinstance(identifiers, list) and isinstance(identifiers[0], Identifier):
            identifiers = [x.id for x in identifiers]
        q = _db.query(Equivalency).distinct().filter(
            or_(Equivalency.input_id.in_(identifiers),
                Equivalency.output_id.in_(identifiers))
        )
        if exclude_ids:
            q = q.filter(~Equivalency.id.in_(exclude_ids))
        return q

class Identifier(Base):
    """A way of uniquely referring to a particular edition.
    """
    
    # Common types of identifiers.
    OVERDRIVE_ID = "Overdrive ID"
    THREEM_ID = "3M ID"
    GUTENBERG_ID = "Gutenberg ID"
    AXIS_360_ID = "Axis 360 ID"
    ASIN = "ASIN"
    ISBN = "ISBN"
    OCLC_WORK = "OCLC Work ID"
    OCLC_NUMBER = "OCLC Number"
    OPEN_LIBRARY_ID = "OLID"
    BIBLIOCOMMONS_ID = "Bibliocommons ID"
    URI = "URI"
    DOI = "DOI"
    UPC = "UPC"

    URN_SCHEME_PREFIX = "urn:librarysimplified.org/terms/id/"
    ISBN_URN_SCHEME_PREFIX = "urn:isbn:"

    __tablename__ = 'identifiers'
    id = Column(Integer, primary_key=True)
    type = Column(String(64), index=True)
    identifier = Column(String, index=True)

    equivalencies = relationship(
        "Equivalency",
        primaryjoin=("Identifier.id==Equivalency.input_id"),
        backref="input_identifiers",
    )

    inbound_equivalencies = relationship(
        "Equivalency",
        primaryjoin=("Identifier.id==Equivalency.output_id"),
        backref="output_identifiers",
    )

    unresolved_identifier = relationship(
        "UnresolvedIdentifier", backref="identifier", uselist=False
    )

    # One Identifier may have many associated CoverageRecords.
    coverage_records = relationship("CoverageRecord", backref="identifier")

    def __repr__(self):
        records = self.primarily_identifies
        if records and records[0].title:
            title = u' wr=%d ("%s")' % (records[0].id, records[0].title)
        else:
            title = ""
        return (u"%s/%s ID=%s%s" % (self.type, self.identifier, self.id,
                                    title)).encode("utf8")

    # One Identifier may serve as the primary identifier for
    # several Editions.
    primarily_identifies = relationship(
        "Edition", backref="primary_identifier"
    )

    # One Identifier may serve as the identifier for
    # a single LicensePool.
    licensed_through = relationship(
        "LicensePool", backref="identifier", uselist=False, lazy='joined',
    )

    # One Identifier may have many Links.
    links = relationship(
        "Hyperlink", backref="identifier"
    )

    # One Identifier may be the subject of many Measurements.
    measurements = relationship(
        "Measurement", backref="identifier"
    )

    # One Identifier may participate in many Classifications.
    classifications = relationship(
        "Classification", backref="identifier"
    )

    # Type + identifier is unique.
    __table_args__ = (
        UniqueConstraint('type', 'identifier'),
    )

    @classmethod
    def from_asin(cls, _db, asin, autocreate=True):
        """Turn an ASIN-like string into an Identifier.

        If the string is an ISBN10 or ISBN13, the Identifier will be
        of type ISBN and the value will be the equivalent ISBN13.

        Otherwise the Identifier will be of type ASIN and the value will
        be the value of `asin`.
        """
        asin = asin.strip()
        if isbnlib.is_isbn10(asin):
            asin = isbnlib.to_isbn13(asin)
        if isbnlib.is_isbn13(asin):
            type = cls.ISBN
        else:
            type = cls.ASIN
        return cls.for_foreign_id(_db, type, asin, autocreate)

    @classmethod
    def for_foreign_id(cls, _db, foreign_identifier_type, foreign_id,
                       autocreate=True):
        """Turn a foreign ID into an Identifier."""
        was_new = None
        if foreign_identifier_type in (
                Identifier.OVERDRIVE_ID, Identifier.THREEM_ID):
            foreign_id = foreign_id.lower()
        if autocreate:
            m = get_one_or_create
        else:
            m = get_one
            was_new = False

        result = m(_db, cls, type=foreign_identifier_type,
                   identifier=foreign_id)
        if isinstance(result, tuple):
            return result
        else:
            return result, False

    @property
    def urn(self):
        identifier_text = urllib.quote(self.identifier)
        if self.type == Identifier.ISBN:
            return self.ISBN_URN_SCHEME_PREFIX + identifier_text
        elif self.type == Identifier.URI:
            return self.identifier
        else:
            identifier_type = urllib.quote(self.type)
            return self.URN_SCHEME_PREFIX + "%s/%s" % (
                identifier_type, identifier_text)

    class UnresolvableIdentifierException(Exception):
        # Raised when an identifier that can't be resolved into a LicensePool
        # is provided in a context that requires a resolvable identifier
        pass

    @classmethod
    def parse_urn(cls, _db, identifier_string, must_support_license_pools=False):
        if identifier_string.startswith("http:") or identifier_string.startswith("https:"):
            type = Identifier.URI
        elif identifier_string.startswith(Identifier.URN_SCHEME_PREFIX):
            identifier_string = identifier_string[len(Identifier.URN_SCHEME_PREFIX):]
            type, identifier_string = map(
                urllib.unquote, identifier_string.split("/", 1))
        elif identifier_string.startswith(Identifier.ISBN_URN_SCHEME_PREFIX):
            type = Identifier.ISBN
            identifier_string = identifier_string[len(Identifier.ISBN_URN_SCHEME_PREFIX):]
            identifier_string = urllib.unquote(identifier_string)
            # Make sure this is a valid ISBN, and convert it to an ISBN-13.
            if not (isbnlib.is_isbn10(identifier_string) or
                    isbnlib.is_isbn13(identifier_string)):
                raise ValueError("%s is not a valid ISBN." % identifier_string)
            if isbnlib.is_isbn10(identifier_string):
                identifier_string = isbnlib.to_isbn13(identifier_string)
        else:
            raise ValueError(
                "Could not turn %s into a recognized identifier." %
                identifier_string)


        if must_support_license_pools:
            try:
                DataSource.license_source_for(_db, type)
            except NoResultFound:
                raise Identifier.UnresolvableIdentifierException()
            
        return cls.for_foreign_id(_db, type, identifier_string)

    def equivalent_to(self, data_source, identifier, strength):
        """Make one Identifier equivalent to another.
        
        `data_source` is the DataSource that believes the two 
        identifiers are equivalent.
        """
        _db = Session.object_session(self)
        eq, new = get_one_or_create(
            _db, Equivalency,
            data_source=data_source,
            input=self,
            output=identifier,
            create_method_kwargs=dict(strength=strength))
        # print "%r==%r p=%.2f" % (self, identifier, strength)
        return eq

    @classmethod
    def recursively_equivalent_identifier_ids(
            cls, _db, identifier_ids, levels=5, threshold=0.50, debug=False):
        """All Identifier IDs equivalent to the given set of Identifier
        IDs at the given confidence threshold.

        This is an inefficient but simple implementation, performing
        one SQL query for each level of recursion.

        Four levels is enough to go from a Gutenberg text to an ISBN.
        Gutenberg ID -> OCLC Work IS -> OCLC Number -> ISBN

        Returns a dictionary mapping each ID in the original to a
        dictionary mapping the equivalent IDs to (confidence, strength
        of confidence) 2-tuples.
        """

        if not identifier_ids:
            return {}

        if isinstance(identifier_ids[0], Identifier):
            identifier_ids = [x.id for x in identifier_ids]

        (working_set, seen_equivalency_ids, seen_identifier_ids,
         equivalents) = cls._recursively_equivalent_identifier_ids(
             _db, identifier_ids, identifier_ids, levels, threshold, debug)

        if debug and working_set:
            # This is not a big deal, but it means we could be getting
            # more IDs by increasing the level.
            print "Leftover working set at level %d." % levels

        return equivalents

    @classmethod
    def _recursively_equivalent_identifier_ids(
            cls, _db, original_working_set, working_set, levels, threshold, debug):

        if levels == 0:
            equivalents = defaultdict(lambda : defaultdict(list))
            for id in original_working_set:
                # Every identifier is unshakeably equivalent to itself.
                equivalents[id][id] = (1, 1000000)
            return (working_set, set(), set(), equivalents)

        if not working_set:
            return working_set, seen_equivalency_ids, seen_identifier_ids

        # First make the recursive call.        
        (working_set, seen_equivalency_ids, seen_identifier_ids,
         equivalents) = cls._recursively_equivalent_identifier_ids(
             _db, original_working_set, working_set, levels-1, threshold, debug)

        if not working_set:
            # We're done.
            return (working_set, seen_equivalency_ids, seen_identifier_ids,
                    equivalents)

        new_working_set = set()
        seen_identifier_ids = seen_identifier_ids.union(working_set)

        equivalencies = Equivalency.for_identifiers(
            _db, working_set, seen_equivalency_ids)
        for e in equivalencies:
            if debug:
                print "%r => %r" % (e.input, e.output)
            seen_equivalency_ids.add(e.id)

            # Signal strength decreases monotonically, so
            # if it dips below the threshold, we can
            # ignore it from this point on.

            # I -> O becomes "I is a precursor of O with distance
            # equal to the I->O strength."
            if e.strength > threshold:
                if debug:
                    print "Strong signal: %r" % e
                
                cls._update_equivalents(
                    equivalents, e.output_id, e.input_id, e.strength, e.votes)
                cls._update_equivalents(
                    equivalents, e.input_id, e.output_id, e.strength, e.votes)
            else:
                if debug:
                    print "Ignoring signal below threshold: %r" % e

            if e.output_id not in seen_identifier_ids:
                # This is our first time encountering the
                # Identifier that is the output of this
                # Equivalency. We will look at its equivalencies
                # in the next round.
                new_working_set.add(e.output_id)
            if e.input_id not in seen_identifier_ids:
                # This is our first time encountering the
                # Identifier that is the input to this
                # Equivalency. We will look at its equivalencies
                # in the next round.
                new_working_set.add(e.input_id)

        if debug:
            print "At level %d."
            print " New working set: %r" % sorted(new_working_set)
            print " %d equivalencies seen so far." % len(seen_equivalency_ids)
            print " %d identifiers seen so far." % len(seen_identifier_ids)
            print " %d equivalents" % len(equivalents)

        if debug and new_working_set:
            print " Here's the new working set:",
            for i in _db.query(Identifier).filter(Identifier.id.in_(new_working_set)):
                print "", i

        surviving_working_set = set()
        for id in original_working_set:
            for new_id in new_working_set:
                for neighbor in list(equivalents[id]):
                    if neighbor == id:
                        continue
                    if neighbor == new_id:
                        # The new ID is directly adjacent to one of
                        # the original working set.
                        surviving_working_set.add(new_id)
                        continue
                    if new_id in equivalents[neighbor]:
                        # The new ID is adjacent to an ID adjacent to
                        # one of the original working set. But how
                        # strong is the signal?
                        o2n_weight, o2n_votes = equivalents[id][neighbor]
                        n2new_weight, n2new_votes = equivalents[neighbor][new_id]
                        new_weight = (o2n_weight * n2new_weight)
                        if new_weight > threshold:
                            equivalents[id][new_id] = (new_weight, o2n_votes + n2new_votes)
                            surviving_working_set.add(new_id)

        if debug:
            print "Pruned %d from working set" % len(surviving_working_set.intersection(new_working_set))
        return (surviving_working_set, seen_equivalency_ids, seen_identifier_ids,
                equivalents)

    @classmethod
    def _update_equivalents(original_working_set, equivalents, input_id,
                            output_id, strength, votes):
        if not equivalents[input_id][output_id]:
            equivalents[input_id][output_id] = (strength, votes)
        else:
            try:
                old_strength, old_votes = equivalents[input_id][output_id]
            except Exception, e:
                set_trace()
            total_strength = (old_strength * old_votes) + (strength * votes)
            total_votes = (old_votes + votes)
            new_strength = total_strength / total_votes
            equivalents[input_id][output_id] = (new_strength, total_votes)

    @classmethod
    def recursively_equivalent_identifier_ids_flat(
            cls, _db, identifier_ids, levels=5, threshold=0.5):
        data = cls.recursively_equivalent_identifier_ids(
            _db, identifier_ids, levels, threshold)
        return cls.flatten_identifier_ids(data)

    @classmethod
    def flatten_identifier_ids(cls, data):
        ids = set()
        for equivalents in data.values():
            ids = ids.union(set(equivalents.keys()))
        return ids

    def equivalent_identifier_ids(self, levels=5, threshold=0.5):
        _db = Session.object_session(self)
        return Identifier.recursively_equivalent_identifier_ids_flat(
            _db, [self.id], levels, threshold)

    def add_link(self, rel, href, data_source, license_pool=None,
                 media_type=None, content=None, content_path=None):
        """Create a link between this Identifier and a (potentially new)
        Resource."""
        _db = Session.object_session(self)

        if license_pool and license_pool.identifier != self:
            raise ValueError(
                "License pool is associated with %r, not %r!" % (
                    license_pool.identifier, self))
        
        # Find or create the Resource.
        if not href:
            href = Hyperlink.generic_uri(data_source, self, rel, content)
        resource, new_resource = get_one_or_create(
            _db, Resource, url=href,
            create_method_kwargs=dict(data_source=data_source)
        )

        # Find or create the Hyperlink.
        link, new_link = get_one_or_create(
            _db, Hyperlink, rel=rel, data_source=data_source,
            identifier=self, resource=resource,
            create_method_kwargs=dict(license_pool=license_pool)
        )

        if content or content_path:
            resource.set_fetched_content(media_type, content, content_path)
        return link, new_link

    def add_measurement(self, data_source, quantity_measured, value,
                        weight=1, taken_at=None):
        """Associate a new Measurement with this Identifier."""
        _db = Session.object_session(self)

        now = datetime.datetime.utcnow()
        taken_at = taken_at or now
        # Is there an existing most recent measurement?
        most_recent = get_one(
            _db, Measurement, identifier=self,
            data_source=data_source,
            quantity_measured=quantity_measured,
            is_most_recent=True, on_multiple='interchangeable'
        )
        if most_recent and most_recent.value == value and taken_at == now:
            # The value hasn't changed since last time. Just update
            # the timestamp of the existing measurement.
            self.taken_at = taken_at

        if most_recent and most_recent.taken_at < taken_at:
            most_recent.is_most_recent = False

        return create(
            _db, Measurement,
            identifier=self, data_source=data_source,
            quantity_measured=quantity_measured, taken_at=taken_at,
            value=value, weight=weight, is_most_recent=True)[0]

    def classify(self, data_source, subject_type, subject_identifier,
                 subject_name=None, weight=1):
        """Classify this Identifier under a Subject.

        :param type: Classification scheme; one of the constants from Subject.
        :param subject_identifier: Internal ID of the subject according to that classification scheme.

        ``value``: Human-readable description of the subject, if different
                   from the ID.

        ``weight``: How confident the data source is in classifying a
                    book under this subject. The meaning of this
                    number depends entirely on the source of the
                    information.
        """
        _db = Session.object_session(self)
        # Turn the subject type and identifier into a Subject.
        classifications = []
        subject, is_new = Subject.lookup(
            _db, subject_type, subject_identifier, subject_name)
        #if is_new:
        #    print repr(subject)

        # Use a Classification to connect the Identifier to the
        # Subject.
        try:
            classification, is_new = get_one_or_create(
                _db, Classification,
                identifier=self,
                subject=subject,
                data_source=data_source)
        except MultipleResultsFound, e:
            # TODO: This is a hack.
            all_classifications = _db.query(Classification).filter(
                Classification.identifier==self,
                Classification.subject==subject,
                Classification.data_source==data_source)
            all_classifications = all_classifications.all()
            classification = all_classifications[0]
            for i in all_classifications[1:]:
                _db.delete(i)

        classification.weight = weight
        return classification

    @classmethod
    def resources_for_identifier_ids(self, _db, identifier_ids, rel=None,
                                     data_source=None):
        resources = _db.query(Resource).join(Resource.links).filter(
                Hyperlink.identifier_id.in_(identifier_ids))
        if data_source:
            resources = resources.filter(Hyperlink.data_source==data_source)
        if rel:
            if isinstance(rel, list):
                resources = resources.filter(Hyperlink.rel.in_(rel))
            else:
                resources = resources.filter(Hyperlink.rel==rel)
        resources = resources.options(joinedload('representation'))
        return resources

    @classmethod
    def classifications_for_identifier_ids(self, _db, identifier_ids):
        classifications = _db.query(Classification).filter(
                Classification.identifier_id.in_(identifier_ids))
        return classifications.options(joinedload('subject'))

    IDEAL_COVER_ASPECT_RATIO = 2.0/3
    IDEAL_IMAGE_HEIGHT = 240
    IDEAL_IMAGE_WIDTH = 160

    # The point at which a generic geometric image is better
    # than some other image.
    MINIMUM_IMAGE_QUALITY = 0.25

    @classmethod
    def best_cover_for(cls, _db, identifier_ids):
        # Find all image resources associated with any of
        # these identifiers.
        images = cls.resources_for_identifier_ids(
            _db, identifier_ids, Hyperlink.IMAGE)
        images = images.join(Resource.representation)
        images = images.filter(Representation.mirrored_at != None).filter(
            Representation.mirror_url != None)
        images = images.all()

        champion = None
        champions = []
        champion_score = None
        # Judge the image resource by its deviation from the ideal
        # aspect ratio, and by its deviation (in the "too small"
        # direction only) from the ideal resolution.
        for r in images:
            for link in r.links:
                if not link.license_pool.open_access:
                    # For licensed works, always present the cover
                    # provided by the licensing authority.
                    r.quality = 1
                    champion = r
                    break

            if champion and champion.quality == 1:
                # No need to look further
                break

            rep = r.representation
            if not rep:
                continue

            if not champion:
                champion = r
                continue

            if not rep.image_width or not rep.image_height:
                continue
            aspect_ratio = rep.image_width / float(rep.image_height)
            aspect_difference = abs(aspect_ratio-cls.IDEAL_COVER_ASPECT_RATIO)
            quality = 1 - aspect_difference
            width_difference = (
                float(rep.image_width - cls.IDEAL_IMAGE_WIDTH) / cls.IDEAL_IMAGE_WIDTH)
            if width_difference < 0:
                # Image is not wide enough.
                quality = quality * (1+width_difference)
            height_difference = (
                float(rep.image_height - cls.IDEAL_IMAGE_HEIGHT) / cls.IDEAL_IMAGE_HEIGHT)
            if height_difference < 0:
                # Image is not tall enough.
                quality = quality * (1+height_difference)

            # Scale the estimated quality by the source of the image.
            source_name = r.data_source.name
            if source_name==DataSource.GUTENBERG_COVER_GENERATOR:
                quality = quality * 0.60
            elif source_name==DataSource.GUTENBERG:
                quality = quality * 0.50
            elif source_name==DataSource.OPEN_LIBRARY:
                quality = quality * 0.25

            r.set_estimated_quality(quality)

            # TODO: that says how good the image is as an image. But
            # how good is it as an image for this particular book?
            # Determining this requires measuring the conceptual
            # distance from the image to a Edition, and then from
            # the Edition to the Work in question. This is much
            # too big a project to work on right now.

            if not r.quality >= cls.MINIMUM_IMAGE_QUALITY:
                continue
            if r.quality > champion_score:
                champions = [r]
                champion_score = r.quality
            elif r.quality == champion_score:
                champions.append(r)
        if champions and not champion:
            champion = random.choice(champions)
            
        return champion, images

    @classmethod
    def evaluate_summary_quality(cls, _db, identifier_ids, 
                                 privileged_data_source=None):
        """Evaluate the summaries for the given group of Identifier IDs.

        This is an automatic evaluation based solely on the content of
        the summaries. It will be combined with human-entered ratings
        to form an overall quality score.

        We need to evaluate summaries from a set of Identifiers
        (typically those associated with a single work) because we
        need to see which noun phrases are most frequently used to
        describe the underlying work.

        :param privileged_data_source: If present, a summary from this
        data source will be instantly chosen, short-circuiting the
        decision process.

        :return: The single highest-rated summary Resource.

        """
        evaluator = SummaryEvaluator()

        # Find all rel="description" resources associated with any of
        # these records.
        rels = [Hyperlink.DESCRIPTION, Hyperlink.SHORT_DESCRIPTION]
        descriptions = cls.resources_for_identifier_ids(
            _db, identifier_ids, rels, privileged_data_source)
        descriptions = descriptions.join(
            Resource.representation).filter(
                Representation.content != None).all()

        champion = None
        # Add each resource's content to the evaluator's corpus.
        for r in descriptions:
            evaluator.add(r.representation.content)
        evaluator.ready()

        # Then have the evaluator rank each resource.
        for r in descriptions:
            content = r.representation.content
            quality = evaluator.score(content)
            r.set_estimated_quality(quality)
            if not champion or r.quality > champion.quality:
                champion = r

        if privileged_data_source and not champion:
            # We could not find any descriptions from the privileged
            # data source. Try relaxing that restriction.
            return cls.evaluate_summary_quality(_db, identifier_ids)
        return champion, descriptions

    @classmethod
    def missing_coverage_from(
            cls, _db, identifier_types, coverage_data_source):
        """Find identifiers of the given types which have no CoverageRecord
        from `coverage_data_source`.
        """
        q = _db.query(Identifier).outerjoin(
            CoverageRecord, Identifier.id==CoverageRecord.identifier_id).filter(
                Identifier.type.in_(identifier_types))
        q2 = q.filter(CoverageRecord.id==None)
        return q2

class UnresolvedIdentifier(Base):
    """An identifier that the metadata wrangler has heard of but hasn't
    yet been able to connect with a book being offered by someone.
    """

    __tablename__ = 'unresolvedidentifiers'
    id = Column(Integer, primary_key=True)

    identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True)

    # A numeric status code, analogous to an HTTP status code,
    # describing the status of the process of resolving this
    # identifier.
    status = Column(Integer, index=True)

    # Timestamp of the first time we tried to resolve this identifier.
    first_attempt = Column(DateTime, index=True)

    # Timestamp of the most recent time we tried to resolve this identifier.
    most_recent_attempt = Column(DateTime, index=True)

    # The problem that's stopping this identifier from being resolved.
    exception = Column(Unicode, index=True)

    @classmethod
    def register(cls, _db, identifier, force=False):
        if identifier.licensed_through and not force:
            # There's already a license pool for this identifier, and
            # thus no need to do anything.
            raise ValueError(
                "%r has already been resolved. Not creating an UnresolvedIdentifier record for it." % identifier)

        try:
            datasource = DataSource.license_source_for(_db, identifier)
        except MultipleResultsFound:
            # This is fine--we'll just try every source we know of until
            # we find one.
            pass
        except NoResultFound:
            # This is not okay--we have no way of resolving this identifier.
            raise Identifier.UnresolvableIdentifierException()

        return get_one_or_create(
            _db, UnresolvedIdentifier, identifier=identifier,
            create_method_kwargs=dict(status=202), on_multiple='interchangeable'
        )

class Contributor(Base):

    """Someone (usually human) who contributes to books."""
    __tablename__ = 'contributors'
    id = Column(Integer, primary_key=True)

    # Standard identifiers for this contributor.
    lc = Column(Unicode, index=True)
    viaf = Column(Unicode, index=True)

    # This is the name by which this person is known in the original
    # catalog. It is sortable, e.g. "Twain, Mark".
    name = Column(Unicode, index=True)
    aliases = Column(ARRAY(Unicode), default=[])

    # This is the name we will display publicly. Ideally it will be
    # the name most familiar to readers.
    display_name = Column(Unicode, index=True)

    # This is a short version of the contributor's name, displayed in
    # situations where the full name is too long. For corporate contributors
    # this value will be None.
    family_name = Column(Unicode, index=True)
    
    # This is the name used for this contributor on Wikipedia. This
    # gives us an entry point to Wikipedia, Wikidata, etc.
    wikipedia_name = Column(Unicode, index=True)


    extra = Column(MutableDict.as_mutable(JSON), default={})

    contributions = relationship("Contribution", backref="contributor")
    work_contributions = relationship("WorkContribution", backref="contributor",
                                      )
    # Types of roles
    AUTHOR_ROLE = "Author"
    PRIMARY_AUTHOR_ROLE = "Primary Author"
    PERFORMER_ROLE = "Performer"
    UNKNOWN_ROLE = 'Unknown'
    AUTHOR_ROLES = set([PRIMARY_AUTHOR_ROLE, AUTHOR_ROLE])

    # Extra fields
    BIRTH_DATE = 'birthDate'
    DEATH_DATE = 'deathDate'

    def __repr__(self):
        extra = ""
        if self.lc:
            extra += " lc=%s" % self.lc
        if self.viaf:
            extra += " viaf=%s" % self.viaf
        return (u"Contributor %d (%s)" % (self.id, self.name)).encode("utf8")

    @classmethod
    def lookup(cls, _db, name=None, viaf=None, lc=None, aliases=None,
               extra=None):
        """Find or create a record for the given Contributor."""
        extra = extra or dict()

        create_method_kwargs = {
            Contributor.name.name : name,
            Contributor.aliases.name : aliases,
            Contributor.extra.name : extra
        }

        if not name and not lc and not viaf:
            raise ValueError(
                "Cannot look up a Contributor without any identifying "
                "information whatsoever!")

        if name and not lc and not viaf:
            # We will not create a Contributor based solely on a name
            # unless there is no existing Contributor with that name.
            #
            # If there *are* contributors with that name, we will
            # return all of them.
            #
            # We currently do not check aliases when doing name lookups.
            q = _db.query(Contributor).filter(Contributor.name==name)
            contributors = q.all()
            if contributors:
                return contributors, False
            else:
                try:
                    contributor = Contributor(**create_method_kwargs)
                    _db.add(contributor)
                    _db.flush()
                    contributors = [contributor]
                    new = True
                except IntegrityError:
                    _db.rollback()
                    contributors = q.all()
                    new = False
        else:
            # We are perfecly happy to create a Contributor based solely
            # on lc or viaf.
            query = dict()
            if lc:
                query[Contributor.lc.name] = lc
            if viaf:
                query[Contributor.viaf.name] = viaf

            try:
                contributors, new = get_one_or_create(
                    _db, Contributor, create_method_kwargs=create_method_kwargs,
                    **query)
            except Exception, e:
                set_trace()

        return contributors, new

    def merge_into(self, destination):
        """Two Contributor records should be the same.

        Merge this one into the other one.

        For now, this should only be used when the exact same record
        comes in through two sources. It should not be used when two
        Contributors turn out to represent different names for the
        same human being, e.g. married names or (especially) pen
        names. Just because we haven't thought that situation through
        well enough.
        """
        if self == destination:
            # They're already the same.
            return
        msg = u"MERGING %s (%s) into %s (%s)" % (
            repr(self).decode("utf8"), self.viaf,
            repr(destination).decode("utf8"),
            destination.viaf)
        print msg.encode("utf8")
        existing_aliases = set(destination.aliases)
        new_aliases = list(destination.aliases)
        for name in [self.name] + self.aliases:
            if name != destination.name and name not in existing_aliases:
                new_aliases.append(name)
        if new_aliases != destination.aliases:
            destination.aliases = new_aliases
        for k, v in self.extra.items():
            if not k in destination.extra:
                destination.extra[k] = v
        if not destination.lc:
            destination.lc = self.lc
        if not destination.viaf:
            destination.viaf = self.viaf
        if not destination.family_name:
            destination.family_name = self.family_name
        if not destination.display_name:
            destination.display_name = self.display_name
        if not destination.wikipedia_name:
            destination.wikipedia_name = self.wikipedia_name

        _db = Session.object_session(self)
        print " Merging edition contributions."
        for contribution in self.contributions:
            # Is the new contributor already associated with this
            # Edition in the given role (in which case we delete
            # the old contribution) or not (in which case we switch the
            # contributor ID)?
            existing_record = _db.query(Contribution).filter(
                Contribution.contributor_id==destination.id,
                Contribution.edition_id==contribution.edition.id,
                Contribution.role==contribution.role)
            if existing_record.count():
                _db.delete(contribution)
            else:
                contribution.contributor_id = destination.id
        print " Merging work contributions."
        for contribution in self.work_contributions:
            existing_record = _db.query(WorkContribution).filter(
                WorkContribution.contributor_id==destination.id,
                WorkContribution.edition_id==contribution.edition.id,
                WorkContribution.role==contribution.role)
            if existing_record.count():
                _db.delete(contribution)
            else:
                contribution.contributor_id = destination.id
            contribution.contributor_id = destination.id
        print "Commit before deletion."
        _db.commit()
        print "Final deletion."
        _db.delete(self)
        print "Committing after deletion."
        _db.commit()
        # _db.query(Contributor).filter(Contributor.id==self.id).delete()
        #_db.commit()
        print "All done."

    # Regular expressions used by default_names().
    PARENTHETICAL = re.compile("\([^)]*\)")
    ALPHABETIC = re.compile("[a-zA-z]")
    NUMBERS = re.compile("[0-9]")

    DATE_RES = [re.compile("\(?" + x + "\)?") for x in 
                "[0-9?]+-",
                "[0-9]+st cent",
                "[0-9]+nd cent",
                "[0-9]+th cent",
                "\bcirca",
                ]


    def default_names(self, default_display_name=None):
        """Attempt to derive a family name ("Twain") and a display name ("Mark
        Twain") from a catalog name ("Twain, Mark").

        This is full of pitfalls, which is why we prefer to use data
        from VIAF. But when there is no data from VIAF, the output of
        this algorithm is better than the input in pretty much every
        case.
        """
        return self._default_names(self.name, default_display_name)

    @classmethod
    def _default_names(cls, name, default_display_name=None):
        original_name = name
        """Split out from default_names to make it easy to test."""
        display_name = default_display_name
        # "Little, Brown &amp; Co." => "Little, Brown & Co."
        name = name.replace("&amp;", "&")

        # "Philadelphia Broad Street Church (Philadelphia, Pa.)"
        #  => "Philadelphia Broad Street Church"
        name = cls.PARENTHETICAL.sub("", name)
        name = name.strip()

        if ', ' in name:
            # This is probably a personal name.
            parts = name.split(", ")
            if len(parts) > 2:
                # The most likely scenario is that the final part
                # of the name is a date or a set of dates. If this
                # seems true, just delete that part.
                if (cls.NUMBERS.search(parts[-1])
                    or not cls.ALPHABETIC.search(parts[-1])):
                    parts = parts[:-1]
            # The final part of the name may have a date or a set
            # of dates at the end. If so, remove it from that string.
            final = parts[-1]
            for date_re in cls.DATE_RES:
                m = date_re.search(final)
                if m:
                    new_part = final[:m.start()].strip() 
                    if new_part:
                        parts[-1] = new_part
                    else:
                        del parts[-1]
                    break
               
            family_name = parts[0]
            p = parts[-1].lower()
            if (p in ('llc', 'inc', 'inc.')
                or p.endswith("company") or p.endswith(" co.")
                or p.endswith(" co")):
                # No, this is a corporate name that contains a comma.
                # It can't be split on the comma, so don't bother.
                family_name = None
                display_name = display_name or name
            if not display_name:
                # The fateful moment. Swap the second string and the
                # first string.
                if len(parts) == 1:
                    display_name = parts[0]
                    family_name = display_name
                else:
                    display_name = parts[1] + " " + parts[0]
                if len(parts) > 2:
                    # There's a leftover bit.
                    if parts[2] in ('Mrs.', 'Mrs', 'Sir'):
                        # "Jones, Bob, Mrs."
                        #  => "Mrs. Bob Jones"
                        display_name = parts[2] + " " + display_name
                    else:
                        # "Jones, Bob, Jr."
                        #  => "Bob Jones, Jr."
                        display_name += ", " + " ".join(parts[2:])
        else:
            # Since there's no comma, this is probably a corporate name.
            family_name = None
            display_name = name
        #print " Default names for %s" % original_name
        #print "  Family name: %s" % family_name
        #print "  Display name: %s" % display_name
        #print
        return family_name, display_name


class Contribution(Base):
    """A contribution made by a Contributor to a Edition."""
    __tablename__ = 'contributions'
    id = Column(Integer, primary_key=True)
    edition_id = Column(Integer, ForeignKey('editions.id'), index=True,
                           nullable=False)
    contributor_id = Column(Integer, ForeignKey('contributors.id'), index=True,
                            nullable=False)
    role = Column(Unicode, index=True, nullable=False)
    __table_args__ = (
        UniqueConstraint('edition_id', 'contributor_id', 'role'),
    )


class WorkContribution(Base):
    """A contribution made by a Contributor to a Work."""
    __tablename__ = 'workcontributions'
    id = Column(Integer, primary_key=True)
    work_id = Column(Integer, ForeignKey('works.id'), index=True,
                     nullable=False)
    contributor_id = Column(Integer, ForeignKey('contributors.id'), index=True,
                            nullable=False)
    role = Column(Unicode, index=True, nullable=False)
    __table_args__ = (
        UniqueConstraint('work_id', 'contributor_id', 'role'),
    )


class Edition(Base):

    """A lightly schematized collection of metadata for a work, or an
    edition of a work, or a book, or whatever. If someone thinks of it
    as a "book" with a "title" it can go in here.
    """

    __tablename__ = 'editions'
    id = Column(Integer, primary_key=True)

    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)

    MAX_THUMBNAIL_HEIGHT = 300

    # This Edition is associated with one particular
    # identifier--the one used by its data source to identify
    # it. Through the Equivalency class, it is associated with a
    # (probably huge) number of other identifiers.
    primary_identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True)

    # A Edition may be associated with a single Work.
    work_id = Column(Integer, ForeignKey('works.id'), index=True)

    # A Edition may be the primary identifier associated with its
    # Work, or it may not be.
    is_primary_for_work = Column(Boolean, index=True, default=False)

    # An Edition may show up in many CustomListEntries.
    custom_list_entries = relationship("CustomListEntry", backref="edition")

    title = Column(Unicode, index=True)
    sort_title = Column(Unicode, index=True)
    subtitle = Column(Unicode, index=True)
    series = Column(Unicode, index=True)

    # This is not a foreign key per se; it's a calculated UUID-like
    # identifier for this work based on its title and author, used to
    # group together different editions of the same work.
    permanent_work_id = Column(Unicode, index=True)

    # A string depiction of the authors' names.
    author = Column(Unicode, index=True)
    sort_author = Column(Unicode, index=True)

    contributions = relationship("Contribution", backref="edition")

    language = Column(Unicode, index=True)
    publisher = Column(Unicode, index=True)
    imprint = Column(Unicode, index=True)

    # `published is the original publication date of the
    # text. `issued` is when made available in this ebook edition. A
    # Project Gutenberg text was likely `published` long before being
    # `issued`.
    issued = Column(Date)
    published = Column(Date)

    BOOK_MEDIUM = "Book"
    PERIODICAL_MEDIUM = "Periodical"
    AUDIO_MEDIUM = "Audio"
    MUSIC_MEDIUM = "Music"
    VIDEO_MEDIUM = "Video"

    medium = Column(
        Enum(BOOK_MEDIUM, PERIODICAL_MEDIUM, AUDIO_MEDIUM,
             MUSIC_MEDIUM, VIDEO_MEDIUM, name="medium"),
        default=BOOK_MEDIUM, index=True
    )

    cover_id = Column(
        Integer, ForeignKey(
            'resources.id', use_alter=True, name='fk_editions_summary_id'), 
        index=True)
    # These two let us avoid actually loading up the cover Resource
    # every time.
    cover_full_url = Column(Unicode)
    cover_thumbnail_url = Column(Unicode)

    # This is set to True if we know there just isn't a cover for this
    # edition. That lets us know it's okay to set the corresponding
    # work to presentation ready even in the absence of a cover for
    # its primary edition.
    no_known_cover = Column(Boolean, default=False)

    # Information kept in here probably won't be used.
    extra = Column(MutableDict.as_mutable(JSON), default={})

    def __repr__(self):
        id_repr = repr(self.primary_identifier).decode("utf8")
        a = (u"Edition %s [%r] (%s/%s/%s)" % (
            self.id, id_repr, self.title,
            ", ".join([x.name for x in self.contributors]),
            self.language))
        try:
            a.encode("utf8")
        except Exception, e:
            set_trace()
        return a.encode("utf8")

    @property
    def language_code(self):
        return LanguageCodes.three_to_two.get(self.language, self.language)

    @property
    def contributors(self):
        return [x.contributor for x in self.contributions]

    @property
    def author_contributors(self):
        """All 'author'-type contributors, with the primary author first,
        other authors sorted by sort name.
        """
        primary_author = None
        other_authors = []
        for x in self.contributions:
            if not primary_author and x.role == Contributor.PRIMARY_AUTHOR_ROLE:
                primary_author = x.contributor
            elif x.role in Contributor.AUTHOR_ROLES:
                other_authors.append(x.contributor)
        if primary_author:
            return [primary_author] + sorted(other_authors, key=lambda x: x.name)
        else:
            return other_authors

    @classmethod
    def for_foreign_id(cls, _db, data_source,
                       foreign_id_type, foreign_id,
                       create_if_not_exists=True):
        """Find the Edition representing the given data source's view of
        the work that it primarily identifies by foreign ID.

        e.g. for_foreign_id(_db, DataSource.OVERDRIVE,
                            Identifier.OVERDRIVE_ID, uuid)

        finds the Edition for Overdrive's view of a book identified
        by Overdrive UUID.

        This:

        for_foreign_id(_db, DataSource.OVERDRIVE, Identifier.ISBN, isbn)

        will probably return nothing, because although Overdrive knows
        that books have ISBNs, it doesn't use ISBN as a primary
        identifier.
        """
        # Look up the data source if necessary.
        if isinstance(data_source, basestring):
            data_source = DataSource.lookup(_db, data_source)

        identifier, ignore = Identifier.for_foreign_id(
            _db, foreign_id_type, foreign_id)

        # Combine the two to get/create a Edition.
        if create_if_not_exists:
            f = get_one_or_create
            kwargs = dict()
        else:
            f = get_one
            kwargs = dict()
        r = f(_db, Edition, data_source=data_source,
                 primary_identifier=identifier,
                 **kwargs)
        return r

    @property
    def license_pool(self):
        """The Edition's corresponding LicensePool, if any.
        """
        _db = Session.object_session(self)
        return get_one(_db, LicensePool,
                       data_source=self.data_source,
                       identifier=self.primary_identifier)

    def equivalencies(self, _db):
        """All the direct equivalencies between this record's primary
        identifier and other Identifiers.
        """
        return self.primary_identifier.equivalencies
        
    def equivalent_identifier_ids(self, levels=3, threshold=0.5):
        """All Identifiers equivalent to this record's primary identifier,
        at the given level of recursion."""
        return self.primary_identifier.equivalent_identifier_ids(
            levels, threshold)

    def equivalent_identifiers(self, levels=3, threshold=0.5, type=None):
        """All Identifiers equivalent to this
        Edition's primary identifier, at the given level of recursion.
        """
        _db = Session.object_session(self)
        identifier_ids = self.equivalent_identifier_ids(levels, threshold)
        q = _db.query(Identifier).filter(
            Identifier.id.in_(identifier_ids))
        if type:
            if isinstance(type, list):
                q = q.filter(Identifier.type.in_(type))
            else:
                q = q.filter(Identifier.type==type)
        return q

    def equivalent_editions(self, levels=5, threshold=0.5):
        """All Editions whose primary ID is equivalent to this Edition's
        primary ID, at the given level of recursion.

        Five levels is enough to go from a Gutenberg ID to an Overdrive ID
        (Gutenberg ID -> OCLC Work ID -> OCLC Number -> ISBN -> Overdrive ID)
        """
        _db = Session.object_session(self)
        identifier_ids = self.equivalent_identifier_ids(levels, threshold)
        return _db.query(Edition).filter(
            Edition.primary_identifier_id.in_(identifier_ids))

    @classmethod
    def missing_coverage_from(
            cls, _db, edition_data_sources, coverage_data_source):
        """Find Editions from `edition_data_source` whose primary
        identifiers have no CoverageRecord from
        `coverage_data_source`.

        e.g.

         gutenberg = DataSource.lookup(_db, DataSource.GUTENBERG)
         oclc_classify = DataSource.lookup(_db, DataSource.OCLC)
         missing_coverage_from(_db, gutenberg, oclc_classify)

        will find Editions that came from Project Gutenberg and
        have never been used as input to the OCLC Classify web
        service.

        """
        if isinstance(edition_data_sources, DataSource):
            edition_data_sources = [edition_data_sources]
        edition_data_source_ids = [x.id for x in edition_data_sources]
        join_clause = ((Edition.primary_identifier_id==CoverageRecord.identifier_id) &
                       (CoverageRecord.data_source_id==coverage_data_source.id))
        
        q = _db.query(Edition).outerjoin(
            CoverageRecord, join_clause).filter(
                Edition.data_source_id.in_(edition_data_source_ids))
        q2 = q.filter(CoverageRecord.id==None)
        return q2


    @classmethod
    def _content(cls, content, is_html=False):
        """Represent content that might be plain-text or HTML.

        e.g. a book's summary.
        """
        if not content:
            return None
        if is_html:
            type = "html"
        else:
            type = "text"
        return dict(type=type, value=content)

    def set_cover(self, resource):
        self.cover = resource
        self.cover_full_url = resource.representation.mirror_url

        # TODO: In theory there could be multiple scaled-down
        # versions of this representation and we need some way of
        # choosing between them. Right now we just pick the first one
        # that works.
        if (resource.representation.image_height
            and resource.representation.image_height <= self.MAX_THUMBNAIL_HEIGHT):
            # This image doesn't need a thumbnail.
            self.cover_thumbnail_url = resource.representation.mirror_url
        else:
            for scaled_down in resource.representation.thumbnails:
                if scaled_down.mirror_url and scaled_down.mirrored_at:
                    self.cover_thumbnail_url = scaled_down.mirror_url
                    break
        print self.cover_full_url, self.cover_thumbnail_url

    def add_contributor(self, name, roles, aliases=None, lc=None, viaf=None,
                        **kwargs):
        """Assign a contributor to this Edition."""
        _db = Session.object_session(self)
        if isinstance(roles, basestring):
            roles = [roles]            

        # First find or create the Contributor.
        if isinstance(name, Contributor):
            contributor = name
        else:
            contributor, was_new = Contributor.lookup(
                _db, name, lc, viaf, aliases)
            if isinstance(contributor, list):
                # Contributor was looked up/created by name,
                # which returns a list.
                contributor = contributor[0]

        # Then add their Contributions.
        for role in roles:
            get_one_or_create(
                _db, Contribution, edition=self, contributor=contributor,
                role=role)
        return contributor

    def similarity_to(self, other_record):
        """How likely is it that this record describes the same book as the
        given record?

        1 indicates very strong similarity, 0 indicates no similarity
        at all.

        For now we just compare the sets of words used in the titles
        and the authors' names. This should be good enough for most
        cases given that there is usually some preexisting reason to
        suppose that the two records are related (e.g. OCLC said
        they were).

        Most of the Editions are from OCLC Classify, and we expect
        to get some of them wrong (e.g. when a single OCLC work is a
        compilation of several novels by the same author). That's okay
        because those Editions aren't backed by
        LicensePools. They're purely informative. We will have some
        bad information in our database, but the clear-cut cases
        should outnumber the fuzzy cases, so we we should still group
        the Editions that really matter--the ones backed by
        LicensePools--together correctly.
        
        TODO: apply much more lenient terms if the two Editions are
        identified by the same ISBN or other unique identifier.
        """
        if other_record == self:
            # A record is always identical to itself.
            return 1

        if other_record.language == self.language:
            # The books are in the same language. Hooray!
            language_factor = 1
        else:
            if other_record.language and self.language:
                # Each record specifies a different set of languages. This
                # is an immediate disqualification.
                return 0
            else:
                # One record specifies a language and one does not. This
                # is a little tricky. We're going to apply a penalty, but
                # since the majority of records we're getting from OCLC are in
                # English, the penalty will be less if one of the
                # languages is English. It's more likely that an unlabeled
                # record is in English than that it's in some other language.
                if self.language == 'eng' or other_record.language == 'eng':
                    language_factor = 0.80
                else:
                    language_factor = 0.50
       
        title_quotient = MetadataSimilarity.title_similarity(
            self.title, other_record.title)

        author_quotient = MetadataSimilarity.author_similarity(
            self.author_contributors, other_record.author_contributors)
        if author_quotient == 0:
            # The two works have no authors in common. Immediate
            # disqualification.
            return 0

        # We weight title more heavily because it's much more likely
        # that one author wrote two different books than that two
        # books with the same title have different authors.
        return language_factor * (
            (title_quotient * 0.80) + (author_quotient * 0.20))

    def apply_similarity_threshold(self, candidates, threshold=0.5):
        """Yield the Editions from the given list that are similar 
        enough to this one.
        """
        for candidate in candidates:
            if self == candidate:
                yield candidate
            else:
                similarity = self.similarity_to(candidate)
                if similarity >= threshold:
                    yield candidate

    @property
    def best_open_access_link(self):
        """Find the best open-access Resource for this LicensePool."""
        open_access = Hyperlink.OPEN_ACCESS_DOWNLOAD

        _db = Session.object_session(self)
        best = None
        q = Identifier.resources_for_identifier_ids(
            _db, [self.primary_identifier.id], open_access)
        for l in q:
            
            if l.representation.media_type.startswith(Representation.EPUB_MEDIA_TYPE):
                best = l
                # A Project Gutenberg-ism: if we find a 'noimages' epub,
                # we'll keep looking in hopes of finding a better one.
                if not 'noimages' in best.representation.mirror_url:
                    break
        return best

    def best_cover_within_distance(self, distance, threshold=0.5):
        _db = Session.object_session(self)
        flattened_data = [self.primary_identifier.id]
        if distance > 0:
            data = Identifier.recursively_equivalent_identifier_ids(
                _db, flattened_data, distance, threshold=threshold)
            flattened_data = Identifier.flatten_identifier_ids(data)

        return Identifier.best_cover_for(_db, flattened_data)

    def calculate_permanent_work_id(self):
        w = WorkIDCalculator
        title = self.title
        if self.subtitle:
            title += (": " + self.subtitle)
        authors = self.author_contributors
        if authors:
            # Only use the primary author.
            author = authors[0].name
        else:
            author = None

        title = w.normalize_title(title)
        author = w.normalize_author(author)

        if self.medium == Edition.BOOK_MEDIUM:
            medium = "book"
        elif self.medium == Edition.AUDIO_MEDIUM:
            medium = "book"
        elif self.medium == Edition.MUSIC_MEDIUM:
            medium = "music"
        elif self.medium == Edition.PERIODICAL_MEDIUM:
            medium = "book"
        elif self.medium == Edition.VIDEO_MEDIUM:
            medium = "movie"

        self.permanent_work_id = WorkIDCalculator.permanent_id(
            title, author, medium)

    def calculate_presentation(self, debug=False):

        # Calling calculate_presentation() on NYT data will actually
        # destroy the presentation, so don't do anything.
        if self.data_source.name == DataSource.NYT:
            return
            
        if not self.sort_title:
            self.sort_title = TitleProcessor.sort_title_for(self.title)
        sort_names = []
        display_names = []
        self.last_update_time = datetime.datetime.utcnow()
        for author in self.author_contributors:
            display_name = author.display_name or author.name
            family_name = author.family_name or author.name
            display_names.append([family_name, display_name])
            sort_names.append(author.name)
        self.author = ", ".join([x[1] for x in sorted(display_names)])
        self.sort_author = " ; ".join(sorted(sort_names))

        self.calculate_permanent_work_id()

        for distance in (0, 5):
            # If there's a cover directly associated with the
            # Edition's primary ID, use it. Otherwise, find the
            # best cover associated with any related identifier.
            best_cover, covers = self.best_cover_within_distance(distance)
            if best_cover:
                if not best_cover.representation:
                    print "WARN: Best cover for %s/%s has no representation!" % (self.primary_identifier.type, self.primary_identifier.identifier)
                else:
                    rep = best_cover.representation
                    if not rep.mirrored_at and not rep.thumbnails:
                        print "WARN: Best cover for %s/%s (%s) was never mirrored or thumbnailed!" % (self.primary_identifier.type, self.primary_identifier.identifier, rep.url)
                self.set_cover(best_cover)
                break

        # Now that everything's calculated, print it out.
        if debug:
            t = u"%s (by %s, pub=%s, pwid=%s)" % (
                self.title, self.author, self.publisher, self.permanent_work_id)
            print t.encode("utf8")
            print " language=%s" % self.language
            if self.cover:
                print " cover=" + self.cover.representation.mirror_url
            print


class WorkGenre(Base):
    """An assignment of a genre to a work."""

    __tablename__ = 'workgenres'
    id = Column(Integer, primary_key=True)
    genre_id = Column(Integer, ForeignKey('genres.id'), index=True)
    work_id = Column(Integer, ForeignKey('works.id'), index=True)
    affinity = Column(Float, index=True, default=0)

    @classmethod
    def from_genre(cls, genre):
        wg = WorkGenre()
        wg.genre = genre
        return wg

    def __repr__(self):
        return "%s (%d%%)" % (self.genre.name, self.affinity*100)


class Work(Base):

    APPEALS_URI = "http://librarysimplified.org/terms/appeals/"

    CHARACTER_APPEAL = "Character"
    LANGUAGE_APPEAL = "Language"
    SETTING_APPEAL = "Setting"
    STORY_APPEAL = "Story"
    UNKNOWN_APPEAL = "Unknown"
    NOT_APPLICABLE_APPEAL = "Not Applicable"
    NO_APPEAL = "None"

    __tablename__ = 'works'
    id = Column(Integer, primary_key=True)

    # One Work may have copies scattered across many LicensePools.
    license_pools = relationship("LicensePool", backref="work", lazy='joined')

    # A single Work may claim many Editions.
    editions = relationship("Edition", backref="work")

    # But for consistency's sake, a Work takes its presentation
    # metadata from a single Edition.

    clause = "and_(Edition.work_id==Work.id, Edition.is_primary_for_work==True)"
    primary_edition = relationship(
        "Edition", primaryjoin=clause, uselist=False, lazy='joined')

    # One Work may participate in many WorkGenre assignments.
    genres = association_proxy('work_genres', 'genre',
                               creator=WorkGenre.from_genre)
    work_genres = relationship("WorkGenre", backref="work",
                               cascade="all, delete-orphan")
    audience = Column(Unicode, index=True)
    fiction = Column(Boolean, index=True)

    summary_id = Column(
        Integer, ForeignKey(
            'resources.id', use_alter=True, name='fk_works_summary_id'), 
        index=True)
    # This gives us a convenient place to store a cleaned-up version of
    # the content of the summary Resource.
    summary_text = Column(Unicode)

    # The overall suitability of this work for unsolicited
    # presentation to a patron. This is a calculated value taking both
    # rating and popularity into account.
    quality = Column(Float, index=True)

    # The overall rating given to this work.
    rating = Column(Float, index=True)

    # The overall current popularity of this work.
    popularity = Column(Float, index=True)

    appeal_type = Enum(CHARACTER_APPEAL, LANGUAGE_APPEAL, SETTING_APPEAL,
                       STORY_APPEAL, NOT_APPLICABLE_APPEAL, NO_APPEAL,
                       UNKNOWN_APPEAL, name="appeal")

    primary_appeal = Column(appeal_type, default=None, index=True)
    secondary_appeal = Column(appeal_type, default=None, index=True)

    appeal_character = Column(Float, default=None, index=True)
    appeal_language = Column(Float, default=None, index=True)
    appeal_setting = Column(Float, default=None, index=True)
    appeal_story = Column(Float, default=None, index=True)

    # The last time the availability or metadata changed for this Work.
    last_update_time = Column(DateTime, index=True)

    # This is set to True once all metadata and availability
    # information has been obtained for this Work. Until this is True,
    # the work will not show up in feeds.
    presentation_ready = Column(Boolean, default=False, index=True)

    # This is the last time we tried to make this work presentation ready.
    presentation_ready_attempt = Column(DateTime, default=None, index=True)

    # This is the error that occured while trying to make this Work
    # presentation ready. Until this is cleared, no further attempt
    # will be made to make the Work presentation ready.
    presentation_ready_exception = Column(Unicode, default=None, index=True)

    # A Work may be merged into one other Work.
    was_merged_into_id = Column(Integer, ForeignKey('works.id'), index=True)
    was_merged_into = relationship("Work", remote_side = [id])

    @property
    def title(self):
        if self.primary_edition:
            return self.primary_edition.title
        return None

    @property
    def sort_title(self):
        return self.primary_edition.sort_title or self.primary_edition.title

    @property
    def subtitle(self):
        return self.primary_edition.subtitle

    @property
    def series(self):
        return self.primary_edition.series

    @property
    def author(self):
        if self.primary_edition:
            return self.primary_edition.author
        return None

    @property
    def sort_author(self):
        return self.primary_edition.sort_author or self.primary_edition.author

    @property
    def language(self):
        if self.primary_edition:
            return self.primary_edition.language
        return None

    @property
    def language_code(self):
        return self.primary_edition.language_code

    @property
    def publisher(self):
        return self.primary_edition.publisher

    @property
    def imprint(self):
        return self.primary_edition.imprint

    @property
    def cover_full_url(self):
        return self.primary_edition.cover_full_url

    @property
    def cover_thumbnail_url(self):
        return self.primary_edition.cover_thumbnail_url

    @property
    def has_open_access_license(self):
        return any(x.open_access for x in self.license_pools)

    def __repr__(self):
        return (u'%s "%s" (%s) %s %s (%s wr, %s lp)' % (
                self.id, self.title, self.author, ", ".join([g.name for g in self.genres]), self.language,
                len(self.editions), len(self.license_pools))).encode("utf8")

    def set_summary(self, resource):
        self.summary = resource
        # TODO: clean up the content
        if resource:
            self.summary_text = resource.representation.content

    CURRENTLY_AVAILABLE = "currently_available"
    ALL = "all"

    @classmethod
    def feed_query(cls, _db, languages, availability=CURRENTLY_AVAILABLE):
        """Return a query against Work suitable for using in OPDS feeds."""
        q = _db.query(Work).join(Work.primary_edition).options(
            joinedload('license_pools').joinedload('data_source'),
            joinedload('work_genres')
        )
        q = q.join(Work.license_pools)
        or_clause = or_(
            LicensePool.open_access==True,
            LicensePool.licenses_owned > 0)
        q = q.filter(or_clause)
        if availability == cls.CURRENTLY_AVAILABLE:
            or_clause = or_(
                LicensePool.open_access==True,
                LicensePool.licenses_available > 0)
            q = q.filter(or_clause)
        q = q.filter(
            Edition.language.in_(languages),
            Work.was_merged_into == None,
            Work.presentation_ready == True,
        )
        return q

    @classmethod
    def with_genre(cls, _db, genre):
        """Find all Works classified under the given genre."""
        if isinstance(genre, basestring):
            genre, ignore = Genre.lookup(_db, genre)
        return _db.query(Work).join(WorkGenre).filter(WorkGenre.genre==genre)

    @classmethod
    def with_no_genres(self, q):
        """Modify a query so it finds only Works that are not classified under
        any genre."""
        q = q.outerjoin(Work.work_genres)
        q = q.filter(WorkGenre.genre==None)
        return q

    def all_editions(self, recursion_level=5):
        """All Editions identified by a Identifier equivalent to 
        any of the primary identifiers of this Work's Editions.

        `recursion_level` controls how far to go when looking for equivalent
        Identifiers.
        """
        _db = Session.object_session(self)
        identifier_ids = self.all_identifier_ids(recursion_level)
        q = _db.query(Edition).filter(
            Edition.primary_identifier_id.in_(identifier_ids))
        return q

    def all_identifier_ids(self, recursion_level=5):
        _db = Session.object_session(self)
        primary_identifier_ids = [
            x.primary_identifier.id for x in self.editions]
        identifier_ids = Identifier.recursively_equivalent_identifier_ids_flat(
            _db, primary_identifier_ids, recursion_level)
        return identifier_ids

    @property
    def language_code(self):
        """A single 2-letter language code for display purposes."""
        if not self.language:
            return None
        language = self.language
        if language in LanguageCodes.three_to_two:
            language = LanguageCodes.three_to_two[language]
        return language

    def similarity_to(self, other_work):
        """How likely is it that this Work describes the same book as the
        given Work (or Edition)?

        This is more accurate than Edition.similarity_to because we
        (hopefully) have a lot of Editions associated with each
        Work. If their metadata has a lot of overlap, the two Works
        are probably the same.
        """
        my_languages = Counter()
        my_authors = Counter()
        total_my_languages = 0
        total_my_authors = 0
        my_titles = []
        other_languages = Counter()
        total_other_languages = 0
        other_titles = []
        other_authors = Counter()
        total_other_authors = 0
        for record in self.editions:
            if record.language:
                my_languages[record.language] += 1
                total_my_languages += 1
            my_titles.append(record.title)
            for author in record.author_contributors:
                my_authors[author] += 1
                total_my_authors += 1

        if isinstance(other_work, Work):
            other_editions = other_work.editions
        else:
            other_editions = [other_work]

        for record in other_editions:
            if record.language:
                other_languages[record.language] += 1
                total_other_languages += 1
            other_titles.append(record.title)
            for author in record.author_contributors:
                other_authors[author] += 1
                total_other_authors += 1

        title_distance = MetadataSimilarity.histogram_distance(
            my_titles, other_titles)

        my_authors = MetadataSimilarity.normalize_histogram(
            my_authors, total_my_authors)
        other_authors = MetadataSimilarity.normalize_histogram(
            other_authors, total_other_authors)

        author_distance = MetadataSimilarity.counter_distance(
            my_authors, other_authors)

        my_languages = MetadataSimilarity.normalize_histogram(
            my_languages, total_my_languages)
        other_languages = MetadataSimilarity.normalize_histogram(
            other_languages, total_other_languages)

        if not other_languages or not my_languages:
            language_factor = 1
        else:
            language_distance = MetadataSimilarity.counter_distance(
                my_languages, other_languages)
            language_factor = 1-language_distance
        title_quotient = 1-title_distance
        author_quotient = 1-author_distance

        return language_factor * (
            (title_quotient * 0.80) + (author_quotient * 0.20))

    def merge_into(self, target_work, similarity_threshold=0.5):
        """This Work is replaced by target_work.

        The two works must be similar to within similarity_threshold,
        or nothing will happen.

        All of this work's Editions will be assigned to target_work,
        and it will be marked as merged into target_work.
        """
        _db = Session.object_session(self)
        similarity = self.similarity_to(target_work)
        if similarity < similarity_threshold:
            print "NOT MERGING %r into %r, similarity is only %.3f." % (
                self, target_work, similarity)
        else:
            print "MERGING %r into %r, similarity is %.3f." % (
                self, target_work, similarity)
            target_work.license_pools.extend(list(self.license_pools))
            target_work.editions.extend(list(self.editions))
            target_work.calculate_presentation()
            print "The resulting work: %r" % target_work
            self.was_merged_into = target_work
            self.license_pools = []
            self.editions = []

    def all_cover_images(self):
        _db = Session.object_session(self)
        primary_identifier_ids = [
            x.primary_identifier.id for x in self.editions]
        data = Identifier.recursively_equivalent_identifier_ids(
            _db, primary_identifier_ids, 5, threshold=0.5)
        flattened_data = Identifier.flatten_identifier_ids(data)
        return Identifier.resources_for_identifier_ids(
            _db, flattened_data, Hyperlink.IMAGE).join(
            Resource.representation).filter(
                Representation.mirrored_at!=None).filter(
                Representation.scaled_at!=None).order_by(
                Resource.quality.desc())

    def all_descriptions(self):
        _db = Session.object_session(self)
        primary_identifier_ids = [
            x.primary_identifier.id for x in self.editions]
        data = Identifier.recursively_equivalent_identifier_ids(
            _db, primary_identifier_ids, 5, threshold=0.5)
        flattened_data = Identifier.flatten_identifier_ids(data)
        return Identifier.resources_for_identifier_ids(
            _db, flattened_data, Hyperlink.DESCRIPTION).filter(
                Resource.content != None).order_by(
                Resource.quality.desc())

    def set_primary_edition(self):
        """Which of this Work's Editions should be used as the default?
        """
        old_primary = self.primary_edition
        champion = None
        old_champion = None

        for wr in self.editions:
            # Something is better than nothing.
            if not champion:
                champion = wr
                continue

            # A edition with no license pool will only be chosen if
            # there is no other alternatice.
            pool = wr.license_pool
            if not pool:
                continue

            # An open-access edition with no usable download link will
            # only be chosen if there is no alternative.
            if pool.open_access and not wr.best_open_access_link:
                continue

            # Open access is better than not.
            if (wr.license_pool.open_access
                and not champion.license_pool.open_access):
                champion = wr
                continue

            # Higher Gutenberg numbers beat lower Gutenberg numbers.
            if (wr.data_source.name == DataSource.GUTENBERG
                and champion.data_source.name == DataSource.GUTENBERG):

                champion_id = int(champion.primary_identifier.identifier)
                competitor_id = int(wr.primary_identifier.identifier)

                if competitor_id > champion_id:
                    champion = wr
                    continue

            # At the moment, anything is better than 3M, because we
            # can't actually check out 3M books.
            if (champion.data_source.name == DataSource.THREEM
                and wr.data_source.name != DataSource.THREEM):
                champion = wr
                continue

            # More licenses is better than fewer.
            if (wr.license_pool.licenses_owned
                > champion.license_pool.licenses_owned):
                champion = wr
                continue

            # More available licenses is better than fewer.
            if (wr.license_pool.licenses_available
                > champion.license_pool.licenses_available):
                champion = wr
                continue

            # Fewer patrons in the hold queue is better than more.
            if (wr.license_pool.patrons_in_hold_queue
                < champion.license_pool.patrons_in_hold_queue):
                champion = wr
                continue

        for edition in self.editions:
            # There can be only one.
            if edition != champion:
                edition.is_primary_for_work = False
            else:
                edition.is_primary_for_work = True

    def calculate_presentation(self, choose_edition=True,
                               classify=True, choose_summary=True,
                               calculate_quality=True, debug=True):
        """Determine the following information:
        
        * Which Edition is the 'primary'. The default view of the
        Work will be taken from the primary Edition.

        * Subject-matter classifications for the work.
        * Whether or not the work is fiction.
        * The intended audience for the work.
        * The best available summary for the work.
        * The overall popularity of the work.
        """
        dirty = False
        if choose_edition or not self.primary_edition:
            self.set_primary_edition()

        # The privileged data source may short-circuit the process of
        # finding a good cover or description.
        if self.primary_edition:
            privileged_data_source = self.primary_edition.data_source

            # We can't use descriptions or covers from Gutenberg, so
            # we never consider it a privileged data source.
            if privileged_data_source.name == DataSource.GUTENBERG:
                privileged_data_source = None
        else:
            privileged_data_source = None

        if self.primary_edition:
            self.primary_edition.calculate_presentation(debug=debug)

        if not (classify or choose_summary or calculate_quality):
            return

        # Find all related IDs that might have associated descriptions
        # or classifications.
        _db = Session.object_session(self)
        primary_identifier_ids = [
            x.primary_identifier.id for x in self.editions]
        data = Identifier.recursively_equivalent_identifier_ids(
            _db, primary_identifier_ids, 5, threshold=0.5)
        flattened_data = Identifier.flatten_identifier_ids(data)

        if classify:
            workgenres, self.fiction, self.audience = self.assign_genres(
                flattened_data)

        if choose_summary:
            summary, summaries = Identifier.evaluate_summary_quality(
                _db, flattened_data, privileged_data_source)
            # TODO: clean up the content
            self.set_summary(summary)

        # If this is a Project Gutenberg or 3M book, treat the number of IDs
        # associated with the work (~the number of editions of the
        # work published in modern times) as a measurement of
        # popularity.
        #
        # TODO: This measurement needs to be scaled with a percentile
        # list, not by crudely dividing by three.
        if privileged_data_source:
            dsn = privileged_data_source.name
            if dsn in (DataSource.GUTENBERG, DataSource.THREEM):
                oclc_linked_data = DataSource.lookup(
                    _db, DataSource.OCLC_LINKED_DATA)
                if dsn == DataSource.GUTENBERG:
                    quotient = 3.0
                else:
                    quotient = 2.0
                self.primary_edition.primary_identifier.add_measurement(
                    oclc_linked_data, Measurement.POPULARITY, 
                    len(flattened_data)/quotient)
            if dsn == DataSource.GUTENBERG:
                # Only consider the quality signals associated with the
                # primary edition. Otherwise texts that have multiple
                # Gutenberg editions will drag down the quality of popular
                # books.
                flattened_data = [self.primary_edition.primary_identifier.id]

        if calculate_quality:
            self.calculate_quality(flattened_data)

        self.last_update_time = datetime.datetime.utcnow()

        # Now that everything's calculated, print it out.
        if debug:
            t = u"WORK %s (by %s)" % (self.title, self.author)
            print t.encode("utf8")
            print " language=%s" % self.language
            print " quality=%s" % self.quality
            if self.fiction:
                fiction = "Fiction"
            elif self.fiction == False:
                fiction = "Nonfiction"
            else:
                fiction = "???"
            print " %(fiction)s a=%(audience)s" % (
                dict(fiction=fiction,
                     audience=self.audience))
            print " " + ", ".join(repr(wg) for wg in self.work_genres)
            if self.summary:
                d = " Description (%.2f) %s" % (
                    self.summary.quality, self.summary.representation.content[:100])
                if isinstance(d, unicode):
                    d = d.encode("utf8")
                print d
            print

    def set_presentation_ready(self, as_of=None):
        as_of = as_of or datetime.datetime.utcnow()
        self.presentation_ready = True
        self.presentation_ready_exception = None
        self.presentation_ready_attempt = as_of

    def set_presentation_ready_based_on_content(self):
        """Set this work as presentation ready, if it appears to
        be ready based on its data.

        Presentation ready means the book is ready to be shown to
        patrons and (pending availability) checked out. It doesn't
        necessarily mean the presentation is complete.

        A work with no summary can still be presentation ready,
        since many public domain books have no summary.

        A work with no cover can be presentation ready 
        """
        if (not self.primary_edition
            or not self.license_pools
            or not self.title
            or not self.primary_edition.author
            or not self.language
            or not self.work_genres
            or (not self.cover_thumbnail_url
                and not self.primary_edition.no_known_cover)):
            self.presentation_ready = False
        else:
            self.set_presentation_ready()

    def calculate_quality(self, flattened_data):
        _db = Session.object_session(self)
        quantities = [Measurement.POPULARITY, Measurement.RATING,
                      Measurement.DOWNLOADS]
        measurements = _db.query(Measurement).filter(
            Measurement.identifier_id.in_(flattened_data)).filter(
                Measurement.is_most_recent==True).filter(
                    Measurement.quantity_measured.in_(quantities)).all()

        self.quality = Measurement.overall_quality(measurements)

    def assign_genres(self, identifier_ids, cutoff=0.15):
        _db = Session.object_session(self)

        classifications = Identifier.classifications_for_identifier_ids(
            _db, identifier_ids)
        fiction_s = Counter()
        genre_s = Counter()
        audience_s = Counter()
        for classification in classifications:
            subject = classification.subject
            if not subject.checked:
                subject.assign_to_genre()
            if (not subject.fiction and not subject.genre
                and not subject.audience):
                continue
            weight = classification.scaled_weight
            fiction_s[subject.fiction] += weight
            if Subject.type == Subject.OVERDRIVE:
                # We trust Overdrive classifications of audience quite
                # a bit. We don't trust 3M classifications more than
                # usual because it doesn't distinguish between
                # childrens' and YA.
                audience_weight = weight * 50
            else:
                audience_weight = weight
            audience_s[subject.audience] += audience_weight
            if subject.genre:
                genre_s[subject.genre] += weight
        if fiction_s[True] > fiction_s[False]:
            fiction = True
        elif fiction_s[False] > fiction_s[True]:
            fiction = False
        else:
            fiction = None
        unmarked = audience_s[None]
        adult = audience_s[Classifier.AUDIENCE_ADULT]
        audience = Classifier.AUDIENCE_ADULT

        # To avoid embarassing situations we will classify works by
        # default as being intended for adults.
        # 
        # To be classified as a young adult or childrens' book, there
        # must be twice as many votes for that status as for the
        # 'adult' status, or, if there are no 'adult' classifications,
        # the default status.
        if adult:
            threshold = adult
        else:
            threshold = unmarked
        threshold *= 2

        if audience_s[Classifier.AUDIENCE_YOUNG_ADULT] > threshold:
            audience = Classifier.AUDIENCE_YOUNG_ADULT
        elif audience_s[Classifier.AUDIENCE_CHILDREN] > threshold:
            audience = Classifier.AUDIENCE_CHILDREN

        # Clear any previous genre assignments.
        for i in self.work_genres:
            _db.delete(i)
        self.work_genres = []

        # Consolidate parent genres into their heaviest subgenre.
        genre_s = Classifier.consolidate_weights(genre_s)
        total_weight = float(sum(genre_s.values()))
        workgenres = []

        # First, strip out the stragglers.
        for g, score in genre_s.items():
            affinity = score / total_weight
            if affinity < cutoff:
                total_weight -= score
                del genre_s[g]

        # Assign WorkGenre objects to the remainder.
        for g, score in genre_s.items():
            affinity = score / total_weight
            if not isinstance(g, Genre):
                g, ignore = Genre.lookup(_db, g.name)
            wg, ignore = get_one_or_create(
                _db, WorkGenre, work=self, genre=g)
            wg.affinity = score/total_weight
            workgenres.append(wg)

        return workgenres, fiction, audience

    def assign_appeals(self, character, language, setting, story,
                       cutoff=0.20):
        """Assign the given appeals to the corresponding database fields,
        as well as calculating the primary and secondary appeal.
        """
        self.appeal_character = character
        self.appeal_language = language
        self.appeal_setting = setting
        self.appeal_story = story

        c = Counter()
        c[self.CHARACTER_APPEAL] = character
        c[self.LANGUAGE_APPEAL] = language
        c[self.SETTING_APPEAL] = setting
        c[self.STORY_APPEAL] = story
        primary, secondary = c.most_common(2)
        if primary[1] > cutoff:
            self.primary_appeal = primary[0]
        else:
            self.primary_appeal = self.UNKNOWN_APPEAL

        if secondary[1] > cutoff:
            self.secondary_appeal = secondary[0]
        else:
            self.secondary_appeal = self.NO_APPEAL

class Measurement(Base):
    """A  measurement of some numeric quantity associated with a
    Identifier.
    """
    __tablename__ = 'measurements'

    # Some common measurement types
    POPULARITY = "http://librarysimplified.org/terms/rel/popularity"
    RATING = "http://schema.org/ratingValue"
    DOWNLOADS = "https://schema.org/UserDownloads"
    PAGE_COUNT = "https://schema.org/numberOfPages"

    GUTENBERG_FAVORITE = "http://librarysimplified.org/terms/rel/lists/gutenberg-favorite"

    # If a book's popularity measurement is found between index n and
    # index n+1 on this list, it is in the nth percentile for
    # popularity and its 'popularity' value should be n * 0.01.
    # 
    # These values are empirically determined and may change over
    # time.
    POPULARITY_PERCENTILES = {
        DataSource.OVERDRIVE : [1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 9, 9, 10, 10, 11, 12, 13, 14, 15, 15, 16, 18, 19, 20, 21, 22, 24, 25, 26, 28, 30, 31, 33, 35, 37, 39, 41, 43, 46, 48, 51, 53, 56, 59, 63, 66, 70, 74, 78, 82, 87, 92, 97, 102, 108, 115, 121, 128, 135, 142, 150, 159, 168, 179, 190, 202, 216, 230, 245, 260, 277, 297, 319, 346, 372, 402, 436, 478, 521, 575, 632, 702, 777, 861, 965, 1100, 1248, 1428, 1665, 2020, 2560, 3535, 5805],
        DataSource.AMAZON : [14937330, 1974074, 1702163, 1553600, 1432635, 1327323, 1251089, 1184878, 1131998, 1075720, 1024272, 978514, 937726, 898606, 868506, 837523, 799879, 770211, 743194, 718052, 693932, 668030, 647121, 627642, 609399, 591843, 575970, 559942, 540713, 524397, 511183, 497576, 483884, 470850, 458438, 444475, 432528, 420088, 408785, 398420, 387895, 377244, 366837, 355406, 344288, 333747, 324280, 315002, 305918, 296420, 288522, 279185, 270824, 262801, 253865, 246224, 238239, 230537, 222611, 215989, 208641, 202597, 195817, 188939, 181095, 173967, 166058, 160032, 153526, 146706, 139981, 133348, 126689, 119201, 112447, 106795, 101250, 96534, 91052, 85837, 80619, 75292, 69957, 65075, 59901, 55616, 51624, 47598, 43645, 39403, 35645, 31795, 27990, 24496, 20780, 17740, 14102, 10498, 7090, 3861],
        # This is a percentile list of OCLC Work IDs and OCLC Numbers
        # associated with Project Gutenberg texts via OCLC Linked
        # Data.
        #
        # TODO: Calculate a separate distribution for more modern works.
        DataSource.OCLC_LINKED_DATA : [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 6, 6, 7, 7, 8, 8, 9, 10, 11, 12, 14, 15, 18, 21, 29, 41, 81],
    }

    DOWNLOAD_PERCENTILES = {
        DataSource.GUTENBERG : [0, 1, 2, 3, 4, 5, 5, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 12, 12, 12, 13, 14, 14, 15, 15, 16, 16, 17, 18, 18, 19, 19, 20, 21, 21, 22, 23, 23, 24, 25, 26, 27, 28, 28, 29, 30, 32, 33, 34, 35, 36, 37, 38, 40, 41, 43, 45, 46, 48, 50, 52, 55, 57, 60, 62, 65, 69, 72, 76, 79, 83, 87, 93, 99, 106, 114, 122, 130, 140, 152, 163, 179, 197, 220, 251, 281, 317, 367, 432, 501, 597, 658, 718, 801, 939, 1065, 1286, 1668, 2291, 4139]
    }

    RATING_SCALES = {
        DataSource.OVERDRIVE : [1, 5],
        DataSource.AMAZON : [1, 5],
    }

    id = Column(Integer, primary_key=True)

    # A Measurement is always associated with some Identifier.
    identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True)

    # A Measurement always comes from some DataSource.
    data_source_id = Column(
        Integer, ForeignKey('datasources.id'), index=True)

    # The quantity being measured.
    quantity_measured = Column(Unicode, index=True)

    # The measurement itself.
    value = Column(Float)

    # The measurement normalized to a 0...1 scale.
    _normalized_value = Column(Float, name="normalized_value")

    # How much weight should be assigned this measurement, relative to
    # other measurements of the same quantity from the same source.
    weight = Column(Float, default=1)

    # When the measurement was taken
    taken_at = Column(DateTime, index=True)
    
    # True if this is the most recent measurement of this quantity for
    # this Identifier.
    #
    is_most_recent = Column(Boolean, index=True)

    def __repr__(self):
        return "%s(%r)=%s (norm=%.2f)" % (
            self.quantity_measured, self.identifier, self.value,
            self.normalized_value or 0)

    @classmethod
    def overall_quality(cls, measurements, popularity_weight=0.3,
                        rating_weight=0.7):
        """Turn a bunch of measurements into an overall measure of quality."""
        if popularity_weight + rating_weight != 1.0:
            raise ValueError(
                "Popularity weight and rating weight must sum to 1! (%.2f + %.2f)" % (
                    popularity_weight, rating_weight)
        )
        popularities = []
        ratings = []
        for m in measurements:
            l = None
            if m.quantity_measured in (cls.POPULARITY, cls.DOWNLOADS):
                l = popularities
            elif m.quantity_measured == cls.RATING:
                l = ratings
            if l is not None:
                l.append(m)
        popularity = cls._average_normalized_value(popularities)
        rating = cls._average_normalized_value(ratings)
        if popularity is None and rating is None:
            # We have absolutely no idea about the quality of this work.
            return 0
        if popularity is not None and rating is None:
            # Our idea of the quality depends entirely on the work's popularity.
            return popularity
        if rating is not None and popularity is None:
            # Our idea of the quality depends entirely on the work's rating.
            return rating

        # We have both popularity and rating.
        final = (popularity * popularity_weight) + (rating * rating_weight)
        print "(%.2f * %.2f) + (%.2f * %.2f) = %.2f" % (
            popularity, popularity_weight, rating, rating_weight, final)
        return final

    @classmethod
    def _average_normalized_value(cls, measurements):
        num_measurements = 0
        measurement_total = 0
        for m in measurements:
            v = m.normalized_value
            if v is None:
                continue
            num_measurements += m.weight
            measurement_total += (v * m.weight)
        if num_measurements:
            return measurement_total / num_measurements
        else:
            return None

    @property
    def normalized_value(self):
        if self._normalized_value:
            pass
        elif not self.value:
            return None
        elif (self.quantity_measured == self.POPULARITY
              and self.data_source.name in self.POPULARITY_PERCENTILES):
            d = self.POPULARITY_PERCENTILES[self.data_source.name]
            position = bisect.bisect_left(d, self.value)
            self._normalized_value = position * 0.01            
        elif (self.quantity_measured == self.DOWNLOADS
              and self.data_source.name in self.DOWNLOAD_PERCENTILES):
            d = self.DOWNLOAD_PERCENTILES[self.data_source.name]
            position = bisect.bisect_left(d, self.value)
            self._normalized_value = position * 0.01            
        elif (self.quantity_measured == self.RATING
              and self.data_source.name in self.RATING_SCALES):
            scale_min, scale_max = self.RATING_SCALES[self.data_source.name]
            width = float(scale_max-scale_min)
            value = self.value-scale_min
            self._normalized_value = value / width

        return self._normalized_value


class Hyperlink(Base):
    """A link between an Identifier and a Resource."""

    __tablename__ = 'hyperlinks'

    # Some common link relations.
    CANONICAL = "canonical"
    OPEN_ACCESS_DOWNLOAD = "http://opds-spec.org/acquisition/open-access"
    IMAGE = "http://opds-spec.org/image"
    THUMBNAIL_IMAGE = "http://opds-spec.org/image/thumbnail"
    SAMPLE = "http://opds-spec.org/acquisition/sample"
    ILLUSTRATION = "http://librarysimplified.org/terms/rel/illustration"
    REVIEW = "http://schema.org/Review"
    DESCRIPTION = "http://schema.org/description"
    SHORT_DESCRIPTION = "http://librarysimplified.org/terms/rel/short-description"
    AUTHOR = "http://schema.org/author"

    # TODO: Is this the appropriate relation?
    DRM_ENCRYPTED_DOWNLOAD = "http://opds-spec.org/acquisition/"

    id = Column(Integer, primary_key=True)

    # A Hyperlink is always associated with some Identifier.
    identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True, nullable=False)

    # The DataSource through which this link was discovered.
    data_source_id = Column(
        Integer, ForeignKey('datasources.id'), index=True, nullable=False)

    # A Resource may also be associated with some LicensePool which
    # controls scarce access to it.
    license_pool_id = Column(
        Integer, ForeignKey('licensepools.id'), index=True)

    # The link relation between the Identifier and the Resource.
    rel = Column(Unicode, index=True, nullable=False)

    # The Resource on the other end of the link.
    resource_id = Column(
        Integer, ForeignKey('resources.id'), index=True, nullable=False)

    @classmethod
    def generic_uri(cls, data_source, identifier, rel, content=None):
        """Create a generic URI for the other end of this hyperlink.

        This is useful for resources that are obtained through means
        other than fetching a single URL via HTTP. It lets us get a
        URI that's most likely unique, so we can create a Resource
        object without violating the uniqueness constraint.

        If the output of this method isn't unique in your situation
        (because the data source provides more than one link with a
        given link relation for a given identifier), you'll need some
        other way of coming up with generic URIs.

        """
        l = [identifier.urn, urllib.quote(data_source.name), urllib.quote(rel)]
        if content:
            m = md5.new()
            if isinstance(content, unicode):
                content = content.encode("utf8")
            m.update(content)
            l.append(m.hexdigest())
        return ":".join(l)


class Resource(Base):
    """An external resource that may be mirrored locally."""

    __tablename__ = 'resources'

    # How many votes is the initial quality estimate worth?
    ESTIMATED_QUALITY_WEIGHT = 5

    id = Column(Integer, primary_key=True)

    # A URI that uniquely identifies this resource. Most of the time
    # this will be an HTTP URL, which is why we're calling it 'url',
    # but it may also be a made-up URI.
    url = Column(Unicode, index=True)

    # Many Editions may choose this resource (as opposed to other
    # resources linked to them with rel="image") as their cover image.
    cover_editions = relationship("Edition", backref="cover", foreign_keys=[Edition.cover_id])

    # Many Works may use this resource (as opposed to other resources
    # linked to them with rel="description") as their summary.
    summary_works = relationship("Work", backref="summary", foreign_keys=[Work.summary_id])

    links = relationship("Hyperlink", backref="resource")

    # The DataSource that is the controlling authority for this Resource.
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)

    # An archived Representation of this Resource.
    representation_id = Column(
        Integer, ForeignKey('representations.id'), index=True)

    # A calculated value for the quality of this resource, based on an
    # algorithmic treatment of its content.
    estimated_quality = Column(Float)

    # The average of human-entered values for the quality of this
    # resource.
    voted_quality = Column(Float)

    # How many votes contributed to the voted_quality value. This lets
    # us scale new votes proportionately while keeping only two pieces
    # of information.
    votes_for_quality = Column(Integer)

    # A combination of the calculated quality value and the
    # human-entered quality value.
    quality = Column(Float, index=True)

    # URL must be unique.
    __table_args__ = (
        UniqueConstraint('url'),
    )

    @property
    def final_url(self):        
        """URL to the final, mirrored version of this resource, suitable
        for serving to the client.

        :return: A URL, or None if the resource has no mirrored
        representation.
        """
        if not self.representation:
            return None
        if not self.representation.mirror_url:
            return None
        return self.representation.mirror_url

    def set_mirrored_elsewhere(self, media_type):
        """We don't need our own copy of this resource's representation--
        a copy of it has been mirrored already.
        """
        _db = Session.object_session(self)
        if not self.representation:
            self.representation, is_new = get_one_or_create(
                _db, Representation, url=self.url, media_type=media_type)
        self.representation.mirror_url = self.url
        self.representation.set_as_mirrored()

    def set_fetched_content(self, media_type, content, content_path):
        """Simulate a successful HTTP request for a representation
        of this resource.

        This is used when the content of the representation is obtained
        through some other means.
        """
        _db = Session.object_session(self)

        if not (content or content_path):
            raise ValueError(
                "One of content and content_path must be specified.")
        if content and content_path:
            raise ValueError(
                "Only one of content and content_path may be specified.")
        representation, is_new = get_one_or_create(
            _db, Representation, url=self.url, media_type=media_type)
        self.representation = representation
        representation.set_fetched_content(content, content_path)

    def set_estimated_quality(self, estimated_quality):
        """Update the estimated quality."""
        self.estimated_quality = estimated_quality
        self.update_quality()

    def add_quality_votes(self, quality, weight=1):
        """Record someone's vote as to the quality of this resource."""
        total_quality = self.voted_quality * self.votes_for_quality
        total_quality += (quality * weight)
        self.votes_for_quality += weight
        self.voted_quality = total_quality / float(self.votes_for_quality)
        self.update_quality()

    def update_quality(self):
        """Combine `estimated_quality` with `voted_quality` to form `quality`.
        """
        estimated_weight = self.ESTIMATED_QUALITY_WEIGHT
        votes_for_quality = self.votes_for_quality or 0
        total_weight = estimated_weight + votes_for_quality

        total_quality = (((self.estimated_quality or 0) * self.ESTIMATED_QUALITY_WEIGHT) + 
                         ((self.voted_quality or 0) * votes_for_quality))
        self.quality = total_quality / float(total_weight)

    def set_representation(self, media_type, content, uri=None,
                           content_path=None):

        if not uri:
            uri = self.generic_uri
        representation, ignore = get_one_or_create(
            _db, Representation, url=uri, media_type=media_type)
        representation.set_fetched_content(content, content_path)
        self.representation = representation
        

class Genre(Base):
    """A subject-matter classification for a book.

    Much, much more general than Classification.
    """
    __tablename__ = 'genres'
    id = Column(Integer, primary_key=True)
    name = Column(Unicode)

    # One Genre may have affinity with many Subjects.
    subjects = relationship("Subject", backref="genre")

    # One Genre may participate in many WorkGenre assignments.
    works = association_proxy('work_genres', 'work')

    work_genres = relationship("WorkGenre", backref="genre", 
                               cascade="all, delete, delete-orphan")

    def __repr__(self):
        return "<Genre %s (%d subjects, %d works, %d subcategories)>" % (
            self.name, len(self.subjects), len(self.works),
            len(classifier.genres[self.name].subgenres))

    @classmethod
    def lookup(cls, _db, name, autocreate=False):
        if autocreate:
            m = get_one_or_create
        else:
            m = get_one
        if isinstance(name, GenreData):
            name = name.name
        result = m(_db, Genre, name=name)
        if isinstance(result, tuple):
            return result
        else:
            return result, False

    @property
    def self_and_subgenres(self):
        _db = Session.object_session(self)
        genres = []
        for genre_data in classifier.genres[self.name].self_and_subgenres:
            genres.append(self.lookup(_db, genre_data.name)[0])
        return genres

    @property
    def default_fiction(self):
        return classifier.genres[self.name].is_fiction

class Subject(Base):
    """A subject under which books might be classified."""

    # Types of subjects.
    LCC = Classifier.LCC              # Library of Congress Classification
    LCSH = Classifier.LCSH            # Library of Congress Subject Headings
    FAST = Classifier.FAST
    DDC = Classifier.DDC              # Dewey Decimal Classification
    OVERDRIVE = Classifier.OVERDRIVE  # Overdrive's classification system
    THREEM = Classifier.THREEM  # 3M's classification system
    TAG = Classifier.TAG   # Folksonomic tags.
    FREEFORM_AUDIENCE = Classifier.FREEFORM_AUDIENCE

    GUTENBERG_BOOKSHELF = Classifier.GUTENBERG_BOOKSHELF
    TOPIC = Classifier.TOPIC
    PLACE = Classifier.PLACE
    PERSON = Classifier.PERSON
    ORGANIZATION = Classifier.ORGANIZATION
    SIMPLIFIED_GENRE = "http://librarysimplified.org/terms/genres/Simplified/"

    by_uri = {
        SIMPLIFIED_GENRE : SIMPLIFIED_GENRE,
        "http://librarysimplified.org/terms/genres/Overdrive/" : OVERDRIVE,
        "http://librarysimplified.org/terms/genres/3M/" : THREEM,
        "http://id.worldcat.org/fast/" : FAST, # I don't think this is official.
        "http://purl.org/dc/terms/LCC" : LCC,
        "http://purl.org/dc/terms/LCSH" : LCSH,
        "http://purl.org/dc/terms/DDC" : DDC,
    }

    uri_lookup = dict()
    for k, v in by_uri.items():
        uri_lookup[v] = k

    __tablename__ = 'subjects'
    id = Column(Integer, primary_key=True)
    # Type should be one of the constants in this class.
    type = Column(Unicode, index=True)

    # Formal identifier for the subject (e.g. "300" for Dewey Decimal
    # System's Social Sciences subject.)
    identifier = Column(Unicode, index=True)

    # Human-readable name, if different from the
    # identifier. (e.g. "Social Sciences" for DDC 300)
    name = Column(Unicode, default=None, index=True)

    # Whether classification under this subject implies anything about
    # the fiction/nonfiction status of a book.
    fiction = Column(Boolean, default=None)

    # Whether classification under this subject implies anything about
    # the book's audience.
    audience = Column(
        Enum("Adult", "Young Adult", "Children", name="audience"),
        default=None)

    # Each Subject may claim affinity with one Genre.
    genre_id = Column(Integer, ForeignKey('genres.id'), index=True)

    # A locked Subject has been reviewed by a human and software will
    # not mess with it without permission.
    locked = Column(Boolean, default=False, index=True)

    # A checked Subject has been reviewed by software and will
    # not be checked again unless forced.
    checked = Column(Boolean, default=False, index=True)

    # One Subject may participate in many Classifications.
    classifications = relationship(
        "Classification", backref="subject"
    )

    # Type + identifier must be unique.
    __table_args__ = (
        UniqueConstraint('type', 'identifier'),
    )

    def __repr__(self):
        if self.name:
            name = u' ("%s")' % self.name
        else:
            name = u""
        if self.audience:
            audience = " audience=%s" % self.audience
        else:
            audience = ""
        if self.fiction:
            fiction = " (Fiction)"
        elif self.fiction == False:
            fiction = " (Nonfiction)"
        else:
            fiction = ""
        if self.genre:
            genre = ' genre="%s"' % self.genre.name
        else:
            genre = ""
        a = u'[%s:%s%s%s%s%s]' % (
            self.type, self.identifier, name, fiction, audience, genre)
        return a.encode("utf8")

    @classmethod
    def lookup(cls, _db, type, identifier, name):
        """Turn a subject type and identifier into a Subject."""
        classifier = Classifier.lookup(type)
        subject, new = get_one_or_create(
            _db, Subject, type=type,
            identifier=identifier,
            create_method_kwargs=dict(
                name=name,
            )
        )
        if name and not subject.name:
            # We just discovered the name of a subject that previously
            # had only an ID.
            subject.name = name
        return subject, new

    @classmethod
    def common_but_not_assigned_to_genre(cls, _db, min_occurances=1000, 
                                         type_restriction=None):
        q = _db.query(Subject).join(Classification).filter(Subject.genre==None)

        if type_restriction:
            q = q.filter(Subject.type==type_restriction)
        q = q.group_by(Subject.id).having(
            func.count(Subject.id) > min_occurances).order_by(
            func.count(Classification.id).desc())
        return q

    @classmethod
    def assign_to_genres(cls, _db, type_restriction=None, force=False,
                         batch_size=1000):
        """Find subjects that have not been checked yet, assign each a
        genre/audience/fiction status if possible, and mark each as
        checked.

        :param type_restriction: Only consider subjects of the given type.
        :param force: Assign a genre to all subjects not just the ones that
                      have been checked.
        :param batch_size: Perform a database commit every time this many
                           subjects have been checked.
        """
        q = _db.query(Subject).filter(Subject.locked==False)

        if type_restriction:
            q = q.filter(Subject.type==type_restriction)

        if not force:
            q = q.filter(Subject.checked==False)

        counter = 0
        for subject in q:
            subject.assign_to_genre()
            counter += 1
            if not counter % batch_size:
                _db.commit()
        _db.commit()

    def assign_to_genre(self):
        """Assign this subject to a genre."""
        classifier = Classifier.classifiers.get(self.type, None)
        if not classifier:
            return
        self.checked = True
        genredata, audience, fiction = classifier.classify(self)
        if genredata:
            _db = Session.object_session(self)
            genre, was_new = Genre.lookup(_db, genredata.name, True)
            self.genre = genre
        if audience:
            self.audience = audience
        if fiction is not None:
            self.fiction = fiction
        if genredata or audience or fiction:
            print self


class Classification(Base):
    """The assignment of a Identifier to a Subject."""
    __tablename__ = 'classifications'
    id = Column(Integer, primary_key=True)
    identifier_id = Column(
        Integer, ForeignKey('identifiers.id'), index=True)
    subject_id = Column(Integer, ForeignKey('subjects.id'), index=True)
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)

    # How much weight the data source gives to this classification.
    weight = Column(Integer)

    @property
    def scaled_weight(self):
        weight = self.weight
        if self.data_source.name == DataSource.OCLC_LINKED_DATA:
            weight = weight / 10.0
        elif self.data_source.name == DataSource.OVERDRIVE:
            weight = weight * 50
        return weight

# Non-database objects.

class LaneList(object):
    """A list of lanes such as you might see in an OPDS feed."""

    def __repr__(self):
        parent = ""
        if self.parent:
            parent = "parent=%s, " % self.parent.name

        return "<LaneList: %slanes=[%s]>" % (
            parent,
            ", ".join([repr(x) for x in self.lanes])
        )

    @classmethod
    def from_description(self, _db, parent_lane, description):
        lanes = LaneList(parent_lane)
        if parent_lane:
            default_fiction = parent_lane.fiction
            default_audience = parent_lane.audience
        else:
            default_fiction = Lane.FICTION_DEFAULT_FOR_GENRE
            default_audience = Classifier.AUDIENCE_ADULT

        for lane_description in description:
            if isinstance(lane_description, GenreData):
                # This very simple lane is the default view for a genre.
                genre = lane_description
                lane = Lane(_db, genre.name, [genre], True, default_fiction,
                            default_audience, parent_lane)
            elif isinstance(lane_description, Lane):
                # The Lane object has already been created.
                lane = lane_description
                lane.parent = parent_lane
            else:
                # A more complicated lane. Its description is a bunch
                # of arguments to the Lane constructor.
                l = lane_description
                lane = Lane(_db, l['full_name'], l.get('genres', []), 
                            l.get('include_subgenres', True),
                            l.get('fiction', default_fiction),
                            l.get('audience', default_audience),
                            parent_lane,
                            l.get('sublanes', []),
                            l.get('display_name', None)
                        )                            
            lanes.add(lane)
            for sublane in lane.sublanes.lanes:
                lanes.add(sublane)

        return lanes

    def __init__(self, parent=None):
        self.parent = parent
        self.lanes = []
        self.by_name = dict()

    def __iter__(self):
        return self.lanes.__iter__()

    def add(self, lane):
        if lane.parent == self.parent:
            self.lanes.append(lane)
        if lane.name in self.by_name:
            raise ValueError("Duplicate lane: %s" % lane.name)
        self.by_name[lane.name] = lane


class Lane(object):

    """A set of books that would go together in a display."""

    UNCLASSIFIED = "unclassified"
    BOTH_FICTION_AND_NONFICTION = "both fiction and nonfiction"
    FICTION_DEFAULT_FOR_GENRE = "fiction default for genre"

    def __repr__(self):
        if self.sublanes.lanes:
            sublanes = " (sublanes=%d)" % len(self.sublanes.lanes)
        else:
            sublanes = ""
        return "<Lane %s%s>" % (self.name, sublanes)

    @classmethod
    def everything(cls, _db):
        """Return a synthetic Lane that matches everything."""
        return Lane(_db, "", [], True, Lane.BOTH_FICTION_AND_NONFICTION,
                    None)

    def __init__(self, _db, full_name, genres, include_subgenres=True,
                 fiction=True, audience=Classifier.AUDIENCE_ADULT,
                 parent=None, sublanes=[], appeal=None, display_name=None):
        self.name = full_name
        self.display_name = display_name or self.name
        self.parent = parent
        self._db = _db
        self.appeal = appeal

        if genres in (None, self.UNCLASSIFIED):
            # We will only be considering works that are not
            # classified under a genre.
            self.genres = None
            self.include_subgenres = None
        else:
            if not isinstance(genres, list):
                genres = [genres]
            # Turn names or GenreData objects into Genre objects. 
            self.genres = []
            for genre in genres:
                if not isinstance(genre, Genre):
                    genre, ignore = Genre.lookup(_db, genre)
                self.genres.append(genre)
            self.include_subgenres=include_subgenres
        self.fiction = fiction
        self.audience = audience
        self.sublanes = LaneList.from_description(_db, self, sublanes)

    def search(self, languages, query):
        """Find works in this lane that match a search query.
        
        TODO: Current implementation is incredibly bad and does
        a direct database search using ILIKE.
        """
        if isinstance(languages, basestring):
            languages = [languages]

        k = "%" + query + "%"
        q = self.works(languages=languages, fiction=None).filter(
            or_(Edition.title.ilike(k),
                Edition.author.ilike(k)))
        q = q.order_by(Work.quality.desc())
        return q

    def quality_sample(
            self, languages, quality_min_start,
            quality_min_rock_bottom, target_size, availability):
        """Randomly select Works from this Lane that meet minimum quality
        criteria.

        Bring the quality criteria as low as necessary to fill a feed
        of the given size, but not below `quality_min_rock_bottom`.
        """
        if isinstance(languages, basestring):
            languages = [languages]

        quality_min = quality_min_start
        previous_quality_min = None
        results = []
        while (quality_min >= quality_min_rock_bottom
               and len(results) < target_size):
            remaining = target_size - len(results)
            query = self.works(languages=languages, availability=availability)
            if quality_min < 0.01:
                quality_min = 0

            query = query.filter(
                Work.quality >= quality_min,
            )

            if previous_quality_min is not None:
                query = query.filter(
                    Work.quality < previous_quality_min)
            start = time.time()
            query = query.order_by(func.random()).limit(remaining)
            #results.extend([x for x in query.all() if x.license_pools])
            results.extend(query.all())
            print "Quality %.1f got %d results for %s in %.2fsec" % (
                quality_min, len(results), self.name, time.time()-start
                )

            if quality_min == quality_min_rock_bottom or quality_min == 0:
                # We can't lower the bar any more.
                break

            # Lower the bar, in case we didn't get enough results.
            previous_quality_min = quality_min
            quality_min *= 0.5
            if quality_min < quality_min_rock_bottom:
                quality_min = quality_min_rock_bottom
        return results


    def works(self, languages, fiction=None, availability=Work.ALL):
        """Find Works that will go together in this Lane.

        Works will:

        * Be in one of the languages listed in `languages`.

        * Be filed under of the genres listed in `self.genres` (or, if
          `self.include_subgenres` is True, any of those genres'
          subgenres).

        * Have the same appeal as `self.appeal`, if `self.appeal` is present.

        * Are intended for the audience in `self.audience`.

        * Are fiction (if `self.fiction` is True), or nonfiction (if fiction
          is false), or of the default fiction status for the genre
          (if fiction==FICTION_DEFAULT_FOR_GENRE and all genres have
          the same default fiction status). If fiction==None, no fiction
          restriction is applied.

        :param fiction: Override the fiction setting found in `self.fiction`.

        """
        q = Work.feed_query(self._db, languages, availability)

        audience = self.audience
        if fiction is None:
            if self.fiction is not None:
                fiction = self.fiction
            else:
                fiction = self.FICTION_DEFAULT_FOR_GENRE

        if self.genres is None and fiction in (True, False, self.UNCLASSIFIED):
            # No genre plus a boolean value for `fiction` means
            # fiction or nonfiction not associated with any genre.
            q = Work.with_no_genres(q)
        elif self.genres is not None:
            # Find works that are assigned to the given genres. This
            # may also turn into a restriction on the fiction status.
            fiction_default_by_genre = (fiction == self.FICTION_DEFAULT_FOR_GENRE)
            if fiction_default_by_genre:
                # Unset `fiction`. We'll set it again when we find out
                # whether we've got fiction or nonfiction genres.
                fiction = None

            genres = []
            for genre in self.genres:
                if self.include_subgenres:
                    genres.extend(genre.self_and_subgenres)
                else:
                    genres.append(genre)

                if fiction_default_by_genre:
                    if fiction is None:
                        fiction = genre.default_fiction
                    elif fiction != genre.default_fiction:
                        raise ValueError(
                            "I was told to use the default fiction restriction, but the genres %r include contradictory fiction restrictions.")
            if genres:
                q = q.join(Work.work_genres)
                q = q.filter(WorkGenre.genre_id.in_([g.id for g in genres]))

        if self.audience != None:
            q = q.filter(Work.audience==self.audience)

        if self.appeal != None:
            q = q.filter(Work.primary_appeal==self.appeal)

        if fiction == self.UNCLASSIFIED:
            q = q.filter(Work.fiction==None)
        elif fiction != self.BOTH_FICTION_AND_NONFICTION:
            q = q.filter(Work.fiction==fiction)
        return q


class WorkFeed(object):
    
    """Identify a certain page in a certain feed."""

    active_facet_for_field = {
        Edition.title : "title",
        Edition.sort_title : "title",
        Edition.sort_author : "author",
        Edition.author : "author"
    }

    CURRENTLY_AVAILABLE = "available"
    ALL = "all"

    def __init__(self, languages, order_by=None,
                 sort_ascending=True,
                 availability=CURRENTLY_AVAILABLE):
        if isinstance(languages, basestring):
            languages = [languages]
        elif not isinstance(languages, list):
            raise ValueError("Invalid value for languages: %r" % languages)
        self.languages = languages
        if not order_by:
            order_by = []
        elif not isinstance(order_by, list):
            order_by = [order_by]
        self.order_by = order_by
        self.sort_ascending = sort_ascending
        if sort_ascending:
            self.sort_operator = operator.__gt__
        else:
            self.sort_operator = operator.__lt__
        # In addition to the given order, we order by author,
        # then title, then work ID.
        for i in (Edition.sort_author, 
                  Edition.sort_title, 
                  Work.id):
            if not i in self.order_by:
                self.order_by.append(i)
        self.active_facet = self.active_facet_for_field.get(order_by[0], None)

        self.availability = availability

    def base_query(self, _db):
        """A query that will return every work that should go in this feed.

        Subject to language and availability settings.

        This may be filtered down further.
        """
        # By default, return every Work in the entire database.
        return Work.feed_query(_db, self.languages, self.availability)

    def page_query(self, _db, last_edition_seen, page_size, extra_filter=None):
        """Turn the base query into a query that retrieves a particular page 
        of works.
        """

        query = self.base_query(_db)
        primary_order_field = self.order_by[0]
        if last_edition_seen:
            # Only find records that show up after the last one seen.
            last_value = getattr(last_edition_seen, primary_order_field.name)
            if last_value:
                # This means works where the primary ordering field has a
                # higher value.
                clause = self.sort_operator(primary_order_field, last_value)

                base_and_clause = (primary_order_field == last_value)
                for next_order_field in self.order_by[1:]:
                    # OR, it means works where all the previous ordering
                    # fields have the same value as the last work seen,
                    # and this next ordering field has a higher value.
                    new_value = getattr(last_edition_seen, next_order_field.name)
                    if new_value != None:
                        clause = or_(clause,
                                     and_(base_and_clause, 
                                          self.sort_operator(next_order_field, new_value)))
                    base_and_clause = and_(base_and_clause,
                                           (next_order_field == new_value))
                query = query.filter(clause)

        if extra_filter is not None:
            query = query.filter(extra_filter)

        if self.sort_ascending:
            m = lambda x: x.asc()
        else:
            m = lambda x: x.desc()

        order_by = [m(x) for x in self.order_by]
        query = query.order_by(*order_by).limit(page_size)
        return query

class LaneFeed(WorkFeed):

    """A WorkFeed where all the works come from a predefined lane."""

    def __init__(self, lane, *args, **kwargs):
        self.lane = lane
        super(LaneFeed, self).__init__(*args, **kwargs)

    def base_query(self, _db):
        return self.lane.works(self.languages, availability=self.availability)


class CustomListFeed(WorkFeed):

    """A WorkFeed where all the works come from one or more custom lists."""

    def __init__(self, custom_lists, languages, on_list_as_of=None, 
                 **kwargs):
        self.custom_lists = custom_lists
        self.on_list_as_of = on_list_as_of
        super(CustomListFeed, self).__init__(languages, **kwargs)

    def base_query(self, _db):

        # TODO: The simplest way to do this is two queries, but it can
        # be optimized to one if it becomes a problem.

        # First, find all works in one of the given lists which also
        # have a permanent work ID.
        custom_list_ids = [x.id for x in self.custom_lists]
        q = _db.query(CustomListEntry).join(CustomListEntry.edition).filter(
            CustomListEntry.list_id.in_(custom_list_ids)).filter(
                Edition.permanent_work_id != None)
        q = q.options(joinedload(CustomListEntry.edition))

        if self.on_list_as_of:
            # The work must have been seen on the given list as
            # recently as the given date.
            on_list_clause = (
                CustomListEntry.most_recent_appearance >= self.on_list_as_of)
            q = q.filter(on_list_clause)
        permanent_work_ids = set([x.edition.permanent_work_id for x in q])
        print "Potentially %s permanent work IDs." % len(permanent_work_ids)

        # Now the second query. Find all works where the primary edition's
        # permanent work ID is in the big list of IDs we got earlier.
        q = Work.feed_query(_db, self.languages, self.availability)
        q = q.join(Work.primary_edition).filter(
            Edition.permanent_work_id.in_(permanent_work_ids))
        return q


class AllCustomListsFromDataSourceFeed(CustomListFeed):

    """A WorkFeed consolidating all custom lists from a given data source."""

    def __init__(self, _db, data_sources, languages, on_list_as_of=None, 
                 **kwargs):
        if isinstance(data_sources, basestring):
            data_sources = [data_sources]
        sources = [DataSource.lookup(_db, x).id for x in data_sources]
        lists = _db.query(CustomList).filter(CustomList.data_source_id.in_(sources))
        super(AllCustomListsFromDataSourceFeed, self).__init__(
            lists, languages, on_list_as_of, **kwargs)


class LicensePool(Base):

    """A pool of undifferentiated licenses for a work from a given source.
    """

    __tablename__ = 'licensepools'
    id = Column(Integer, primary_key=True)

    # A LicensePool may be associated with a Work. (If it's not, no one
    # can check it out.)
    work_id = Column(Integer, ForeignKey('works.id'), index=True)

    # Each LicensePool is associated with one DataSource and one
    # Identifier, and therefore with one original Edition.
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)
    identifier_id = Column(Integer, ForeignKey('identifiers.id'), index=True)

    # One LicensePool may be associated with one RightsStatus.
    rightsstatus_id = Column(
        Integer, ForeignKey('rightsstatus.id'), index=True)

    # One LicensePool can have many Loans.
    loans = relationship('Loan', backref='license_pool')

    # One LicensePool can have many Holds.
    holds = relationship('Hold', backref='license_pool')

    # One LicensePool can have many CirculationEvents
    circulation_events = relationship(
        "CirculationEvent", backref="license_pool")

    # One LicensePool may have many associated Hyperlinks.
    links = relationship("Hyperlink", backref="license_pool")

    # The date this LicensePool first became available.
    availability_time = Column(DateTime, index=True)

    open_access = Column(Boolean)
    last_checked = Column(DateTime, index=True)
    licenses_owned = Column(Integer,default=0)
    licenses_available = Column(Integer,default=0)
    licenses_reserved = Column(Integer,default=0)
    patrons_in_hold_queue = Column(Integer,default=0)

    # A Identifier should have at most one LicensePool.
    __table_args__ = (UniqueConstraint('identifier_id'),)

    @classmethod
    def for_foreign_id(self, _db, data_source, foreign_id_type, foreign_id):
        """Create a LicensePool for the given foreign ID."""

        # Get the DataSource.
        if isinstance(data_source, basestring):
            data_source = DataSource.lookup(_db, data_source)

        # The data source must be one that offers licenses.
        if not data_source.offers_licenses:
            raise ValueError(
                'Data source "%s" does not offer licenses.' % data_source.name)

        # The type of the foreign ID must be the primary identifier
        # type for the data source.
        if foreign_id_type != data_source.primary_identifier_type:
            raise ValueError(
                "License pools for data source '%s' are keyed to "
                "identifier type '%s' (not '%s', which was provided)" % (
                    data_source.name, data_source.primary_identifier_type,
                    foreign_id_type
                )
            )
 
        # Get the Identifier.
        identifier, ignore = Identifier.for_foreign_id(
            _db, foreign_id_type, foreign_id
            )

        # Get the LicensePool that corresponds to the DataSource and
        # the Identifier.
        license_pool, was_new = get_one_or_create(
            _db, LicensePool, data_source=data_source, identifier=identifier)
        if was_new and not license_pool.availability_time:
            now = datetime.datetime.utcnow()
            license_pool.availability_time = now
        return license_pool, was_new

    def edition(self):
        """The LicencePool's primary Edition.

        This is (our view of) the book's entry on whatever website
        hosts the licenses.
        """
        _db = Session.object_session(self)
        return get_one(_db, Edition,
            data_source=self.data_source,
            primary_identifier=self.identifier)

    @classmethod
    def with_no_work(cls, _db):
        """Find LicensePools that have no corresponding Work."""
        return _db.query(LicensePool).outerjoin(Work).filter(
            Work.id==None).all()

    def add_link(self, rel, href, data_source, media_type=None,
                 content=None, content_path=None):
        """Add a link between this LicensePool and a Resource.

        :param rel: The relationship between this LicensePooland the resource
               on the other end of the link.
        :param href: The URI of the resource on the other end of the link.
        :param media_type: Media type of the representation associated
               with the resource.
        :param content: Content of the representation associated with the
               resource.
        :param content_path: Path (relative to DATA_DIRECTORY) of the
               representation associated with the resource.
        """
        return self.identifier.add_link(
            rel, href, data_source, self, media_type, content, content_path)

    def needs_update(self):
        """Is it time to update the circulation info for this license pool?"""
        now = datetime.datetime.utcnow()
        if not self.last_checked:
            # This pool has never had its circulation info checked.
            return True
        maximum_stale_time = self.data_source.extra.get(
            'circulation_refresh_rate_seconds')
        if maximum_stale_time is None:
            # This pool never needs to have its circulation info checked.
            return False
        age = now - self.last_checked
        return age > maximum_stale_time

    def update_availability(
            self, new_licenses_owned, new_licenses_available, 
            new_licenses_reserved, new_patrons_in_hold_queue):
        """Update the LicensePool with new availability information.
        Log the implied changes as CirculationEvents.
        """

        _db = Session.object_session(self)
        now = datetime.datetime.utcnow()

        for old_value, new_value, more_event, fewer_event in (
                [self.patrons_in_hold_queue,  new_patrons_in_hold_queue,
                 CirculationEvent.HOLD_PLACE, CirculationEvent.HOLD_RELEASE], 
                [self.licenses_available, new_licenses_available,
                 CirculationEvent.CHECKIN, CirculationEvent.CHECKOUT], 
                [self.licenses_reserved, new_licenses_reserved,
                 CirculationEvent.AVAILABILITY_NOTIFY, None], 
                [self.licenses_owned, new_licenses_owned,
                 CirculationEvent.LICENSE_ADD,
                 CirculationEvent.LICENSE_REMOVE]):
            if old_value == new_value:
                continue

            if old_value < new_value:
                event_name = more_event
            else:
                event_name = fewer_event

            if not event_name:
                continue

            CirculationEvent.log(
                _db, self, event_name, old_value, new_value, now)

        # Update the license pool with the latest information.
        self.licenses_owned = new_licenses_owned
        self.licenses_available = new_licenses_available
        self.licenses_reserved = new_licenses_reserved
        self.patrons_in_hold_queue = new_patrons_in_hold_queue
        self.last_checked = now

        # Update the last update time of the Work.
        if self.work:
            self.work.last_update_time = now

    def set_rights_status(self, uri, name=None):
        _db = Session.object_session(self)
        status, ignore = get_one_or_create(
            _db, RightsStatus, uri=uri,
            create_method_kwargs=dict(name=name))
        self.rights_status = status
        return status

    def loan_to(self, patron, start=None, end=None):
        _db = Session.object_session(patron)
        kwargs = dict(start=start or datetime.datetime.utcnow(),
                      end=end)
        return get_one_or_create(
            _db, Loan, patron=patron, license_pool=self, 
            create_method_kwargs=kwargs)

    def on_hold_to(self, patron, start=None, end=None, position=None):
        _db = Session.object_session(patron)
        start = start or datetime.datetime.utcnow()
        hold, new = get_one_or_create(
            _db, Hold, patron=patron, license_pool=self)
        hold.update(start, end, position)
        return hold, new

    @classmethod
    def consolidate_works(cls, _db, calculate_work_even_if_no_author=False):
        """Assign a (possibly new) Work to every unassigned LicensePool."""
        a = 0
        for unassigned in cls.with_no_work(_db):
            etext, new = unassigned.calculate_work(
                even_if_no_author=calculate_work_even_if_no_author)
            if not etext:
                # We could not create a work for this LicensePool,
                # most likely because it does not yet have any
                # associated Edition.
                continue
            a += 1
            print "Created %r" % etext
            if a and not a % 100:
                _db.commit()

    def calculate_work(self, even_if_no_author=False):
        """Try to find an existing Work for this LicensePool.

        If there are no Works for the permanent work ID associated
        with this LicensePool's primary edition, create a new Work.

        Pools that are not open-access will always have a new Work
        created for them.

        :param even_if_no_author: Ordinarily this method will refuse
        to create a Work for a LicensePool whose Edition has no title
        or author. But sometimes a book just has no known author. If
        that's really the case, pass in even_if_no_author=True and the
        Work will be created.
        """
        
        print "Calculating work for %r" % self.edition()
        if self.work:
            # The work has already been done.
            print " Already got one."
            return self.work, False

        primary_edition = self.edition()
        if not primary_edition:
            # We don't have any information about the identifier
            # associated with this LicensePool, so we can't create a work.
            print "WARN: NO EDITION for %s, cowardly refusing to create work." % (
                self.identifier)

            return None, False

        if not primary_edition.title or not primary_edition.author:
            print " Calculating presentation."
            primary_edition.calculate_presentation()



        if not primary_edition.work and (
                not primary_edition.title or (
                    not primary_edition.author and not even_if_no_author)):
            print " Edition has no author or title, not assigning Work to Edition."
            # msg = u"WARN: NO TITLE/AUTHOR for %s/%s/%s/%s, cowardly refusing to create work." % (
            #    self.identifier.type, self.identifier.identifier,
            #    primary_edition.title, primary_edition.author)
            #print msg.encode("utf8")
            return None, False

        if not primary_edition.permanent_work_id:
            primary_edition.calculate_permanent_work_id()

        if primary_edition.work:
            # This pool's primary edition is already associated with
            # a Work. Use that Work.
            work = primary_edition.work

        else:
            _db = Session.object_session(self)
            work = None
            if self.open_access:
                # Is there already an open-access Work which includes editions
                # with this edition's permanent work ID?
                q = _db.query(Edition).filter(
                    Edition.permanent_work_id
                    ==primary_edition.permanent_work_id).filter(
                        Edition.work != None).filter(
                            Edition.id != primary_edition.id)
                for edition in q:
                    if edition.work.has_open_access_license:
                        work = edition.work
                        break

        if work:
            created = False
        else:
            # There is no better choice than creating a brand new Work.
            created = True
            print " NEW WORK for %r" % primary_edition.title
            work = Work()
            _db = Session.object_session(self)
            _db.add(work)
            _db.flush()

        # Associate this LicensePool and its Edition with the work we
        # chose or created.
        work.license_pools.append(self)
        primary_edition.work = work

        # Recalculate the display information for the Work, since the
        # associated Editions have changed.
        work.calculate_presentation()

        if created:
            print " Created %r" % work
        # All done!
        return work, created

    @property
    def best_license_link(self):
        """Find the best available licensing link for the work associated
        with this LicensePool.
        """
        wr = self.edition()
        link = wr.best_open_access_link
        if link:
            return self, link

        # Either this work is not open-access, or there was no epub
        # link associated with it.
        work = self.work
        for pool in work.license_pools:
            wr = pool.edition()
            link = wr.best_open_access_link
            if link:
                return pool, link
        return self, None


class RightsStatus(Base):

    """The terms under which a book has been made available to the general
    public.

    This will normally be 'in copyright', or 'public domain', or a
    Creative Commons license.
    """

    # Currently in copyright.
    IN_COPYRIGHT = "http://librarysimplified.org/terms/rights-status/in-copyright"

    # Public domain in the USA.
    PUBLIC_DOMAIN_USA = "http://librarysimplified.org/terms/rights-status/public-domain-usa"

    # Public domain in some unknown territory
    PUBLIC_DOMAIN_UNKNOWN = "http://librarysimplified.org/terms/rights-status/public-domain-unknown"

    # Unknown copyright status.
    UNKNOWN = "http://librarysimplified.org/terms/rights-status/unknown"

    __tablename__ = 'rightsstatus'
    id = Column(Integer, primary_key=True)

    # A URI unique to the license. This may be a URL (e.g. Creative
    # Commons)
    uri = Column(String, index=True)

    # Human-readable name of the license.
    name = Column(String, index=True)

    # One RightsStatus may apply to many LicensePools.
    licensepools = relationship("LicensePool", backref="rights_status")

class CirculationEvent(Base):

    """Changes to a license pool's circulation status.

    We log these so we can measure things like the velocity of
    individual books.
    """
    __tablename__ = 'circulationevents'

    id = Column(Integer, primary_key=True)

    # One LicensePool can have many circulation events.
    license_pool_id = Column(
        Integer, ForeignKey('licensepools.id'), index=True)

    type = Column(String(32))
    start = Column(DateTime, index=True)
    end = Column(DateTime)
    old_value = Column(Integer)
    delta = Column(Integer)
    new_value = Column(Integer)
    foreign_patron_id = Column(String)

    # A given license pool can only have one event of a given type for
    # a given patron at a given time.
    __table_args__ = (UniqueConstraint('license_pool_id', 'type', 'start',
                                       'foreign_patron_id'),)

    # Constants for use in logging circulation events to JSON
    SOURCE = "source"
    TYPE = "event"

    # The names of the circulation events we recognize.
    CHECKOUT = "check_out"
    CHECKIN = "check_in"
    HOLD_PLACE = "hold_place"
    HOLD_RELEASE = "hold_release"
    LICENSE_ADD = "license_add"
    LICENSE_REMOVE = "license_remove"
    AVAILABILITY_NOTIFY = "availability_notify"
    CIRCULATION_CHECK = "circulation_check"
    SERVER_NOTIFICATION = "server_notification"
    TITLE_ADD = "title_add"
    TITLE_REMOVE = "title_remove"
    UNKNOWN = "unknown"

    # The time format used when exporting to JSON.
    TIME_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

    @classmethod
    def log(cls, _db, license_pool, event_name, old_value, new_value,
            start=None, end=None, foreign_patron_id=None):
        if new_value is None or old_value is None:
            delta = None
        else:
            delta = new_value - old_value
        if not start:
            start = datetime.datetime.utcnow()
        if not end:
            end = start
        print " EVENT %s %s=>%s" % (event_name, old_value, new_value)
        event, was_new = get_one_or_create(
            _db, CirculationEvent, license_pool=license_pool,
            type=event_name, start=start, foreign_patron_id=foreign_patron_id,
            create_method_kwargs=dict(
                old_value=old_value,
                new_value=new_value,
                delta=delta,
                end=end)
            )
        return event, was_new


class Credential(Base):
    """A place to store credentials for external services."""
    __tablename__ = 'credentials'
    id = Column(Integer, primary_key=True)
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)
    patron_id = Column(Integer, ForeignKey('patrons.id'), index=True)
    credential = Column(String)
    expires = Column(DateTime)

    __table_args__ = (
        UniqueConstraint('data_source_id', 'patron_id'),
    )

    @classmethod
    def lookup(self, _db, data_source, patron, refresher_method):
        if isinstance(data_source, basestring):
            data_source = DataSource.lookup(_db, data_source)
        credential, is_new = get_one_or_create(
            _db, Credential, data_source=data_source, patron=patron)
        if (is_new or not credential.expires 
            or credential.expires <= datetime.datetime.utcnow()):
            refresher_method(credential)
        return credential


class Timestamp(Base):
    """A general-purpose timestamp for external services."""

    __tablename__ = 'timestamps'
    service = Column(String(255), primary_key=True)
    timestamp = Column(DateTime)

    @classmethod
    def stamp(self, _db, service):
        now = datetime.datetime.utcnow()
        stamp, was_new = get_one_or_create(
            _db, Timestamp,
            service=service,
            create_method_kwargs=dict(timestamp=now))
        if not was_new:
            stamp.timestamp = now
        return stamp

class Representation(Base):
    """A cached document obtained from (and possibly mirrored to) the Web
    at large.

    Sometimes this is a DataSource's representation of a specific
    book.

    Sometimes it's associated with a database Resource (which has a
    well-defined relationship to one specific book).

    Sometimes it's just a web page that we need a cached local copy
    of.
    """

    EPUB_MEDIA_TYPE = "application/epub+zip"
    TEXT_XML_MEDIA_TYPE = "text/xml"
    APPLICATION_XML_MEDIA_TYPE = "application/xml"
    JPEG_MEDIA_TYPE = "image/jpeg"

    __tablename__ = 'representations'
    id = Column(Integer, primary_key=True)

    # URL from which the representation was fetched.
    url = Column(Unicode, index=True)

    # The media type of the representation.
    media_type = Column(Unicode)

    resource = relationship("Resource", backref="representation", uselist=False)

    ### Records of things we tried to do with this representation.

    # When the representation was last fetched from `url`.
    fetched_at = Column(DateTime, index=True)

    # A textual description of the error encountered the last time
    # we tried to fetch the representation
    fetch_exception = Column(Unicode, index=True)

    # A URL under our control to which this representation will be
    # mirrored.
    mirror_url = Column(Unicode, index=True)

    # When the representation was last pushed to `mirror_url`.
    mirrored_at = Column(DateTime, index=True)
    
    # An exception that happened while pushing this representation
    # to `mirror_url.
    mirror_exception = Column(Unicode, index=True)

    # If this image is a scaled-down version of some other image,
    # `scaled_at` is the time it was last generated.
    scaled_at = Column(DateTime, index=True)

    # If this image is a scaled-down version of some other image,
    # this is the exception that happened the last time we tried
    # to scale it down.
    scale_exception = Column(Unicode, index=True)

    ### End records of things we tried to do with this representation.

    # An image Representation may be a thumbnail version of another
    # Representation.
    thumbnail_of_id = Column(
        Integer, ForeignKey('representations.id'), index=True)

    thumbnails = relationship(
        "Representation",
        backref=backref("thumbnail_of", remote_side = [id]),
        lazy="joined")

    # The HTTP status code from the last fetch.
    status_code = Column(Integer)

    # A textual representation of the HTTP headers sent along with the
    # representation.
    headers = Column(Unicode)

    # The Location header from the last representation.
    location = Column(Unicode)

    # The Last-Modified header from the last representation.
    last_modified = Column(Unicode)

    # The Etag header from the last representation.
    etag = Column(Unicode)

    # The size of the representation, in bytes.
    file_size = Column(Integer)
    
    # If this representation is an image, the height of the image.
    image_height = Column(Integer, index=True)

    # If this representation is an image, the width of the image.
    image_width = Column(Integer, index=True)

    # The content of the representation itself.
    content = Column(Binary)

    # Instead of being stored in the database, the content of the
    # representation may be stored on a local file relative to the
    # data root.
    local_content_path = Column(Unicode)

    # At any given time, we will have a single representation for a
    # given URL and media type.
    __table_args__ = (
        UniqueConstraint('url', 'media_type'),
    )

    # A User-Agent to use when acting like a web browser.
    BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/37.0.2049.0 Safari/537.36 (Simplified)"

    @property
    def age(self):
        if not self.fetched_at:
            return 1000000
        return (datetime.datetime.utcnow() - self.fetched_at).total_seconds()

    @property
    def has_content(self):
        if self.content and self.status_code == 200 and self.fetch_exception is None:
            return True
        if self.local_content_path and os.path.exists(self.local_content_path) and self.fetch_exception is None:
            return True
        return False

    @classmethod
    def get(cls, _db, url, do_get=None, extra_request_headers=None,
            accept=None,
            max_age=None, pause_before=0, allow_redirects=True, debug=True):
        """Retrieve a representation from the cache if possible.
        
        If not possible, retrieve it from the web and store it in the
        cache.
        
        :param do_get: A function that takes arguments (url, headers)
        and retrieves a representation over the network.

        :param max_age: A timedelta object representing the maximum
        time to consider a cached representation fresh. (We ignore the
        caching directives from web servers because they're usually
        far too conservative for our purposes.)

        :return: A 2-tuple (representation, obtained_from_cache)

        """
        do_get = do_get or cls.simple_http_get

        representation = None

        # TODO: We allow representations of the same URL in different
        # media types, but we don't have a good solution here for
        # doing content negotiation (letting the caller ask for a
        # specific set of media types and matching against what we
        # have cached). Fortunately this isn't an issue with any of
        # the data sources we currently use, so for now we can treat
        # different representations of a URL as interchangeable.

        a = dict(url=url)
        if accept:
            a['media_type'] = accept
        representation = get_one(_db, Representation, 'interchangeable', **a)

        # Convert a max_age timedelta to a number of seconds.
        if isinstance(max_age, datetime.timedelta):
            max_age = max_age.total_seconds()

        # Do we already have a usable representation?
        usable_representation = (
            representation and not representation.fetch_exception)

        # Assuming we have a usable representation, is it
        # fresh?
        fresh_representation = (
            usable_representation and (
                max_age is None or max_age > representation.age))

        if fresh_representation:
            if debug:
                print "Cached %s" % url
            return representation, True

        # We have a representation that is either not fresh or not usable.
        # We must make an HTTP request.
        if debug:
            print "Fetching %s" % url
        headers = {}
        if extra_request_headers:
            headers.update(extra_request_headers)
        if accept:
            headers['Accept'] = accept

        if usable_representation:
            # We have a representation but it's not fresh. We will
            # be making a conditional HTTP request to see if there's
            # a new version.
            if representation.last_modified:
                headers['If-Modified-Since'] = representation.last_modified
            if representation.etag:
                headers['If-None-Match'] = representation.etag

        fetched_at = datetime.datetime.utcnow()
        if pause_before:
            time.sleep(pause_before)
        media_type = None
        try:
            status_code, headers, content = do_get(url, headers)
            exception = None
            if 'content-type' in headers:
                media_type = headers['content-type'].lower()
            else:
                media_type = None
        except Exception, e:
            # This indicates there was a problem with making the HTTP
            # request, not that the HTTP request returned an error
            # condition.
            exception = str(e)
            status_code = None
            headers = None
            content = None
            media_type = None

        # At this point we can create a Representation object if there
        # isn't one already.
        if not usable_representation:
            representation, is_new = get_one_or_create(
                _db, Representation, url=url, media_type=media_type)

        representation.fetch_exception = exception
        representation.fetched_at = fetched_at

        if status_code == 304:
            # The representation hasn't changed since we last checked.
            # Set its fetched_at property and return the cached
            # version as though it were new.
            representation.fetched_at = fetched_at
            return representation, False

        if status_code:
            status_code_series = status_code / 100
        else:
            status_code_series = None

        if status_code_series in (2,3) or status_code in (404, 410):
            # We have a new, good representation. Update the
            # Representation object and return it as fresh.
            representation.status_code = status_code
            representation.content = content
            representation.media_type = media_type

            for header, field in (
                    ('etag', 'etag'),
                    ('last-modified', 'last_modified'),
                    ('location', 'location')):
                if header in headers:
                    value = headers[header]
                else:
                    value = None
                setattr(representation, field, value)

            representation.headers = cls.headers_to_string(headers)
            representation.content = content          
            representation.update_image_size()
            return representation, False

        # Okay, things didn't go so well.
        date_string = fetched_at.strftime("%Y-%m-%d %H:%M:%S")
        representation.fetch_exception = representation.fetch_exception or (
            "Most recent fetch attempt (at %s) got status code %s" % (
                date_string, status_code))
        if usable_representation:
            # If we have a usable (but stale) representation, we'd
            # rather return the cached data than destroy the information.
            return representation, True

        # We didn't have a usable representation before, and we still don't.
        # At this point we're just logging an error.
        representation.status_code = status_code
        representation.headers = cls.headers_to_string(headers)
        representation.content = content
        return representation, False

    def update_image_size(self):
        """Make sure .image_height and .image_width are up to date.
       
        Clears .image_height and .image_width if the representation
        is not an image.
        """
        if self.media_type and self.media_type.startswith('image/'):
            image = self.as_image()
            self.image_width, self.image_height = image.size
            # print "%s is %dx%d" % (self.url, self.image_width, self.image_height)
        else:
            self.image_width = self.image_height = None

    @classmethod
    def normalize_content_path(cls, content_path, base=None):
        if not content_path:
            return None
        base = base or os.environ['DATA_DIRECTORY']
        if content_path.startswith(base):
            content_path = content_path[len(base):]
            if content_path.startswith('/'):
                content_path = content_path[1:]
        return content_path

    def set_fetched_content(self, content, content_path=None):
        """Simulate a successful HTTP request for this representation.

        This is used when the content of the representation is obtained
        through some other means.
        """
        if isinstance(content, unicode):
            content = content.encode("utf8")
        self.content = content

        self.local_content_path = self.normalize_content_path(content_path)
        self.status_code = 200
        self.fetched_at = datetime.datetime.utcnow()
        self.fetch_exception = None
        self.update_image_size()


    def set_as_mirrored(self):
        """Record the fact that the representation has been mirrored
        to its .mirror_url.
        """
        self.mirrored_at = datetime.datetime.utcnow()
        self.mirror_exception = None

    @classmethod
    def headers_to_string(cls, d):
        if d is None:
            return None
        return json.dumps(dict(d))

    @classmethod
    def simple_http_get(cls, url, headers, **kwargs):
        """The most simple HTTP-based GET."""
        if not 'timeout' in kwargs:
            kwargs['timeout'] = 20
        
        if not 'allow_redirects' in kwargs:
            kwargs['allow_redirects'] = True
        response = requests.get(url, headers=headers, **kwargs)
        return response.status_code, response.headers, response.content

    @classmethod
    def http_get_no_timeout(cls, url, headers, **kwargs):
        return Representation.simple_http_get(url, headers, timeout=None, **kwargs)

    @classmethod
    def http_get_no_redirect(cls, url, headers, **kwargs):
        """HTTP-based GET with no redirects."""
        return cls.simple_http_get(url, headers, allow_redirects=False, **kwargs)

    @classmethod
    def browser_http_get(cls, url, headers, **kwargs):
        """GET the representation that would be displayed to a web browser.
        """
        headers = dict(headers)
        headers['User-Agent'] = cls.BROWSER_USER_AGENT
        return cls.simple_http_get(url, headers, **kwargs)

    @property
    def is_image(self):
        return self.media_type and self.media_type.startswith("image/")

    @property
    def local_path(self):
        """Return the full local path to the representation on disk."""
        if not self.local_content_path:
            return None
        return os.path.join(os.environ['DATA_DIRECTORY'],
                            self.local_content_path)

    def content_fh(self):
        """Return an open filehandle to the representation's contents.

        This works whether the representation is kept in the database
        or in a file on disk.
        """
        if self.content:
            return StringIO(self.content)
        else:
            if not os.path.exists(self.local_path):
                raise ValueError("%s does not exist." % local_path)
            return open(self.local_path)
            

    def as_image(self):
        """Load this Representation's contents as a PIL image."""
        if not self.is_image:
            raise ValueError(
                "Cannot load non-image representation as image: type %s." 
                % self.media_type)
        if not self.content and not self.local_path:
            raise ValueError("Image representation has no content.")
        return Image.open(self.content_fh())

    pil_format_for_media_type = {
        "image/gif": "gif",
        "image/png": "png",
        "image/jpeg": "jpeg",
    }

    def scale(self, max_height, max_width,
              destination_url, destination_media_type, force=False):
        """Return a Representation that's a scaled-down version of this
        Representation, creating it if necessary.

        :param destination_url: The URL the scaled-down resource will
        (eventually) be uploaded to.

        :return: A 2-tuple (Representation, is_new)

        """
        _db = Session.object_session(self)

        if not destination_media_type in self.pil_format_for_media_type:
            raise ValueError("Unsupported destination media type: %s" % destination_media_type)
                
        pil_format = self.pil_format_for_media_type[destination_media_type]

        # Make sure we actually have an image to scale.
        try:
            image = self.as_image()
        except Exception, e:
            self.scale_exception = traceback.format_exc()
            self.scaled_at = None
            # This most likely indicates an error during the fetch
            # phrase.
            self.fetch_exception = "Error found while scaling: %s" % (
                self.scale_exception)
            print self.scale_exception
            return self, False

        # Now that we've loaded the image, take the opportunity to set
        # the image size of the original representation.
        self.image_width, self.image_height = image.size

        # If the image is already thumbnail-size, don't bother.
        if self.image_height <= max_height and self.image_width <= max_width:
            self.thumbnails = []
            return self, False

        # Do we already have a representation for the given URL?
        thumbnail, is_new = get_one_or_create(
            _db, Representation, url=destination_url, 
            media_type=destination_media_type
        )
        if thumbnail not in self.thumbnails:
            thumbnail.thumbnail_of = self

        if not is_new and not force:
            # We found a preexisting thumbnail and we're allowed to
            # use it.
            return thumbnail, is_new

        # At this point we have a parent Representation (self), we
        # have a Representation that will contain a thumbnail
        # (thumbnail), and we know we need to actually thumbnail the
        # parent into the thumbnail.
        #
        # Because the representation of this image is being
        # changed, it will need to be mirrored later on.
        now = datetime.datetime.utcnow()
        thumbnail.mirror_url = thumbnail.url
        thumbnail.mirrored_at = None
        thumbnail.mirror_exception = None

        args = [(max_width, max_height),
                Image.ANTIALIAS]
        try:
            image.thumbnail(*args)
        except IOError, e:
            # I'm not sure why, but sometimes just trying
            # it again works.
            original_exception = traceback.format_exc()
            try:
                image.thumbnail(*args)
            except IOError, e:
                self.scale_exception = original_exception
                self.scaled_at = None
                return self, False

        # Save the thumbnail image to the database under
        # thumbnail.content.
        output = StringIO()
        if image.mode != 'RGB':
            image = image.convert('RGB')
        try:
            image.save(output, pil_format)
        except Exception, e:
            self.scale_exception = traceback.format_exc()
            self.scaled_at = None
            # This most likely indicates a problem during the fetch phase,
            # Set fetch_exception so we'll retry the fetch.
            self.fetch_exception = "Error found while scaling: %s" % (self.scale_exception)
            return self, False
        thumbnail.content = output.getvalue()
        thumbnail.image_width, thumbnail.image_height = image.size
        output.close()
        thumbnail.scale_exception = None
        thumbnail.scaled_at = now
        return thumbnail, True


class CustomList(Base):
    """A custom grouping of Editions."""

    STAFF_PICKS_NAME = "Staff Picks"

    __tablename__ = 'customlists'
    id = Column(Integer, primary_key=True)
    primary_language = Column(Unicode, index=True)
    data_source_id = Column(Integer, ForeignKey('datasources.id'), index=True)
    foreign_identifier = Column(Unicode, index=True)
    name = Column(Unicode)
    description = Column(Unicode)
    created = Column(DateTime, index=True)
    updated = Column(DateTime, index=True)
    responsible_party = Column(Unicode)

    entries = relationship(
        "CustomListEntry", backref="customlist", lazy="joined")

    # TODO: It should be possible to associate a CustomList with an
    # audience, fiction status, and subject, but there is no planned
    # interface for managing this.

    def add_entry(self, edition, annotation=None, first_appearance=None):
        first_appearance = first_appearance or datetime.datetime.utcnow()
        _db = Session.object_session(self)
        entry, was_new = get_one_or_create(
            _db, CustomListEntry,
            customlist=self, edition=edition,
            create_method_kwargs=dict(first_appearance=first_appearance)
        )
        if (not entry.most_recent_appearance 
            or entry.most_recent_appearance < first_appearance):
            entry.most_recent_appearance = first_appearance
        entry.annotation = annotation
        return entry, was_new

class CustomListEntry(Base):

    __tablename__ = 'customlistentries'
    id = Column(Integer, primary_key=True)    
    
    list_id = Column(Integer, ForeignKey('customlists.id'), index=True)
    edition_id = Column(Integer, ForeignKey('editions.id'), index=True)
    annotation = Column(Unicode)

    # These two fields are for best-seller lists. Even after a book
    # drops off the list, the fact that it once was on the list is
    # still relevant.
    first_appearance = Column(DateTime, index=True)
    most_recent_appearance = Column(DateTime, index=True)

from sqlalchemy.sql import compiler
from psycopg2.extensions import adapt as sqlescape

def dump_query(query):
    dialect = query.session.bind.dialect
    statement = query.statement
    comp = compiler.SQLCompiler(dialect, statement)
    comp.compile()
    enc = dialect.encoding
    params = {}
    for k,v in comp.params.iteritems():
        if isinstance(v, unicode):
            v = v.encode(enc)
        params[k] = sqlescape(v)
    return (comp.string.encode(enc) % params).decode(enc)
