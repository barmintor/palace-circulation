import math
from typing import Dict, List

from core.external_search import ExternalSearchIndex
from core.lane import Facets, Pagination, SearchFacets, WorkList
from core.model.classification import Genre, Subject
from core.model.contributor import Contribution, Contributor
from core.model.edition import Edition
from core.model.licensing import LicensePool
from core.model.resource import Hyperlink
from core.model.work import Work


class OPDS2Feed:
    pass


class OPDS2Annotator:
    """Annotate a feed following the OPDS2 spec"""

    def __init__(self, url, facets, pagination, library, title="OPDS2 Feed") -> None:
        self.url = url
        self.facets: Facets = facets
        self.library = library
        self.title = title
        self.pagination = pagination or Pagination()

    # Should this be in an annotator??
    def metadata_for_work(self, work: Work) -> Dict:
        """Create the metadata json for a work item
        using the schema https://readium.org/webpub-manifest/schema/metadata.schema.json"""
        # TODO: What happens when there is not presentation edition?
        edition: Edition = work.presentation_edition
        pool = self._pool_for_library(edition)
        result = {}
        # Palace marketplace has this as '@type'
        result["@type"] = Edition.medium_to_additional_type.get(edition.medium)
        result["title"] = edition.title
        result["subtitle"] = edition.subtitle
        result["identifier"] = edition.primary_identifier.identifier
        result["sortAs"] = edition.sort_title
        result.update(self._contributors(edition))
        result["language"] = edition.language_code

        subjects = []
        genre: Genre
        for genre in work.genres:
            subjects.append(
                {
                    "scheme": Subject.SIMPLIFIED_GENRE,
                    "name": genre.name,
                    "sortAs": genre.name,
                }
            )
        if subjects:
            result["subject"] = subjects

        # TODO: numberOfPages. we don't store this
        # TODO: duration. we don't store this
        # TODO: abridged. we don't store this
        if edition.publisher:
            result["publisher"] = {"name": edition.publisher}
        if edition.imprint:
            result["imprint"] = {"name": edition.imprint}
        result["modified"] = work.last_update_time
        if pool:
            result["published"] = pool.availability_time
        result["description"] = work.summary_text

        belongs_to = {}
        if work.series:
            belongs_to["series"] = {
                "name": work.series,
                "position": work.series_position
                if work.series_position is not None
                else 1,
            }

        if belongs_to:
            result["belongsTo"] = belongs_to

        # TODO: Collection, what does this stand for?

        links = self._work_metadata_links(edition)
        image_links = self.resource_links(
            edition, Hyperlink.IMAGE, Hyperlink.THUMBNAIL_IMAGE, Hyperlink.ILLUSTRATION
        )

        return dict(metadata=result, links=links, images=image_links)

    def _work_metadata_links(self, edition: Edition):
        """Create links for works in the publication"""
        samples = self.resource_links(edition, Hyperlink.SAMPLE)
        open_access = self.resource_links(edition, Hyperlink.OPEN_ACCESS_DOWNLOAD)
        loan_link = self.loan_link(edition)
        self_link = self.self_link(edition)
        links = []
        if open_access:
            links.extend(open_access)
        if samples:
            links.extend(samples)
        if loan_link:
            links.append(loan_link)
        if self_link:
            links.append(self_link)
        return links

    def resource_links(self, edition: Edition, *rels) -> List[Dict]:
        link: Hyperlink
        samples = []
        for link in edition.primary_identifier.links:
            if link.rel in rels:
                samples.append(
                    {
                        "href": link.resource.url,
                        "rel": link.rel,
                        "type": link.resource.representation.media_type,
                    }
                )
        return samples

    def loan_link(self, edition: Edition) -> Dict:
        return None

    def self_link(self, edition: Edition) -> Dict:
        return None

    def _pool_for_library(self, edition: Edition) -> LicensePool:
        collection_ids = [c.id for c in self.library.all_collections]
        for pool in edition.license_pools:
            if pool.collection_id in collection_ids:
                return pool

    def _contributors(self, edition: Edition) -> Dict:
        authors = {}
        contribution: Contribution
        key_mapping = {
            Contributor.PRIMARY_AUTHOR_ROLE: "author",
            Contributor.TRANSLATOR_ROLE: "translator",
            Contributor.EDITOR_ROLE: "editor",
            Contributor.ILLUSTRATOR_ROLE: "illustrator",
            Contributor.ARTIST_ROLE: "artist",
            Contributor.COLORIST_ROLE: "colorist",
            Contributor.INKER_ROLE: "inker",
            Contributor.PENCILER_ROLE: "pencilor",
            Contributor.LETTERER_ROLE: "letterer",
            Contributor.NARRATOR_ROLE: "narrator",
            Contributor.CONTRIBUTOR_ROLE: "contributor",
        }
        for contribution in edition.contributions:
            if contribution.role in key_mapping:
                contributor = contribution.contributor
                meta = {"name": contributor.display_name}
                if len(contributor.aliases) > 0:
                    meta["additionalName"] = contributor.aliases[0]

                # TODO: Marketplace adds links for the author based search
                # should we do the same?
                authors[key_mapping[contribution.role]] = meta
        return authors

    def feed_links(self):
        # TODO: Next page, previous page, last page
        # Need to get entire sample size from the search db
        # in order to achieve this
        links = [
            {"href": self.url, "rel": "self", "type": "application/opds+json"},
        ]

        return links

    def feed_metadata(self):
        return {
            "title": self.title,
            "itemsPerPage": self.pagination.size,
            "currentPage": math.ceil(self.pagination.offset / self.pagination.size)
            + 1,  # start from 1
        }


class FeedTypes:
    PUBLICATIONS = "publications"


class AcquisitonFeedOPDS2(OPDS2Feed):
    @classmethod
    def publications(
        cls,
        _db,
        url: str,
        worklist: WorkList,
        facets: SearchFacets,
        pagination: Pagination,
        search_engine: ExternalSearchIndex,
        annotator: OPDS2Annotator,
    ):
        # do some caching magic
        # then do the publication

        return cls._generate_publications(
            _db, url, worklist, facets, pagination, search_engine, annotator
        )

    @classmethod
    def _generate_publications(
        cls,
        _db,
        url: str,
        worklist: WorkList,
        facets: SearchFacets,
        pagination: Pagination,
        search_engine: ExternalSearchIndex,
        annotator: OPDS2Annotator,
    ):
        publications = []

        for work in worklist.works(_db, facets=facets, search_engine=search_engine):
            publications.append(work)

        return cls(
            _db,
            url,
            "publications",
            publications,
            annotator,
        )

    def __init__(
        self,
        _db,
        url,
        title,
        works: List[Work],
        annotator: OPDS2Annotator,
        feed_type=FeedTypes.PUBLICATIONS,
    ):
        self._db = _db
        self.url = url
        self.works = works
        self.annotator = annotator
        self.feed_type = feed_type

    def json(self):
        if self.feed_type == FeedTypes.PUBLICATIONS:
            return self.publications_json()

    def publications_json(self):
        result = {}

        entries = []
        for work in self.works:
            entries.append(self.annotator.metadata_for_work(work))

        result["publications"] = entries
        feed_links = self.annotator.feed_links()
        result["links"] = feed_links
        return result

    def __str__(self):
        """Make the serialized OPDS2 feed"""