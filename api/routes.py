import logging
import os
from functools import update_wrapper, wraps

import flask
from flask import Response, make_response, request
from flask_babel import lazy_gettext as _
from flask_cors.core import get_cors_options, set_cors_headers
from werkzeug.exceptions import HTTPException

from core.app_server import ErrorHandler, compressible, returns_problem_detail
from core.model import HasSessionCache
from core.util.problem_detail import ProblemDetail

from .app import app, babel
from .config import Configuration
from .controller import CirculationManager
from .problem_details import REMOTE_INTEGRATION_FAILED


@app.before_first_request
def initialize_circulation_manager():
    if os.environ.get("AUTOINITIALIZE") == "False":
        # It's the responsibility of the importing code to set app.manager
        # appropriately.
        pass
    else:
        if getattr(app, "manager", None) is None:
            try:
                app.manager = CirculationManager(app._db)
            except Exception:
                logging.exception("Error instantiating circulation manager!")
                raise
            # Make sure that any changes to the database (as might happen
            # on initial setup) are committed before continuing.
            app.manager._db.commit()


@babel.localeselector
def get_locale():
    languages = Configuration.localization_languages()
    return request.accept_languages.best_match(languages)


@app.after_request
def print_cache(response):
    if hasattr(app, "_db") and HasSessionCache.CACHE_ATTRIBUTE in app._db.info:
        log = logging.getLogger("core.model.hassessioncache")
        for cls, cache in app._db.info[HasSessionCache.CACHE_ATTRIBUTE].items():
            log.debug(f"{cls}: {cache.stats.hits}/{cache.stats.misses} hits/misses")
    return response


@app.teardown_request
def shutdown_session(exception):
    if hasattr(app, "manager") and hasattr(app.manager, "_db") and app.manager._db:
        if exception:
            app.manager._db.rollback()
        else:
            app.manager._db.commit()


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        patron = app.manager.index_controller.authenticated_patron_from_request()
        if isinstance(patron, ProblemDetail):
            return patron.response
        elif isinstance(patron, Response):
            return patron
        else:
            return f(*args, **kwargs)

    return decorated


def allows_auth(f):
    """Decorator function for a controller method that supports both
    authenticated and unauthenticated requests.

    NOTE: This decorator might not be necessary; you can probably call
    BaseCirculationManagerController.request_patron instead.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        # Try to authenticate a patron. This will set flask.request.patron
        # if and only if there is an authenticated patron.
        app.manager.index_controller.authenticated_patron_from_request()

        # Call the decorated function regardless of whether
        # authentication succeeds.
        return f(*args, **kwargs)

    return decorated


# The allows_patron_web decorator will add Cross-Origin Resource Sharing
# (CORS) headers to routes that will be used by the patron web interface.
# This is necessary for a JS app on a different domain to make requests.
#
# This is mostly taken from the cross_origin decorator in flask_cors, but we
# can't use that decorator because we aren't able to look up the patron web
# client url configuration setting at the time we create the decorator.
def allows_patron_web(f):
    # Override Flask's default behavior and intercept the OPTIONS method for
    # every request so CORS headers can be added.
    f.required_methods = getattr(f, "required_methods", set())
    f.required_methods.add("OPTIONS")
    f.provide_automatic_options = False

    def wrapped_function(*args, **kwargs):
        if request.method == "OPTIONS":
            resp = app.make_default_options_response()
        else:
            resp = make_response(f(*args, **kwargs))

        patron_web_domains = app.manager.patron_web_domains
        if patron_web_domains:
            options = get_cors_options(
                app, dict(origins=patron_web_domains, supports_credentials=True)
            )
            set_cors_headers(resp, options)

        return resp

    return update_wrapper(wrapped_function, f)


h = ErrorHandler(app, app.config["DEBUG"])


@app.errorhandler(Exception)
@allows_patron_web
def exception_handler(exception):
    if isinstance(exception, HTTPException):
        # This isn't an exception we need to handle, it's werkzeug's way
        # of interrupting normal control flow with a specific HTTP response.
        # Return the exception and it will be used as the response.
        return exception
    return h.handle(exception)


def has_library(f):
    """Decorator to extract the library short name from the arguments."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if "library_short_name" in kwargs:
            library_short_name = kwargs.pop("library_short_name")
        else:
            library_short_name = None
        library = app.manager.index_controller.library_for_request(library_short_name)
        if isinstance(library, ProblemDetail):
            return library.response
        else:
            return f(*args, **kwargs)

    return decorated


def has_library_through_external_loan_identifier(
    parameter_name="external_loan_identifier",
):
    """Decorator to get a library using the loan's external identifier.

    :param parameter_name: Name of the parameter holding the loan's external identifier
    :type parameter_name: string

    :return: Decorated function
    :rtype: Callable
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if parameter_name in kwargs:
                external_loan_identifier = kwargs[parameter_name]
            else:
                external_loan_identifier = None

            library = (
                app.manager.index_controller.library_through_external_loan_identifier(
                    external_loan_identifier
                )
            )

            if isinstance(library, ProblemDetail):
                return library.response
            else:
                return func(*args, **kwargs)

        return wrapper

    return decorator


def allows_library(f):
    """Decorator similar to @has_library but if there is no library short name,
    then don't set the request library.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        if "library_short_name" in kwargs:
            library_short_name = kwargs.pop("library_short_name")
            library = app.manager.index_controller.library_for_request(
                library_short_name
            )
            if isinstance(library, ProblemDetail):
                return library.response
        else:
            library = None

        return f(*args, **kwargs)

    return decorated


def library_route(path, *args, **kwargs):
    """Decorator to creates routes that have a library short name in either
    a subdomain or a url path prefix. If not used with @has_library, the view function
    must have a library_short_name argument.
    """

    def decorator(f):
        # This sets up routes for both the subdomain and the url path prefix.
        # The order of these determines which one will be used by url_for -
        # in this case it's the prefix route.
        # We may want to have a configuration option to specify whether to
        # use a subdomain or a url path prefix.
        prefix_route = app.route("/<library_short_name>" + path, *args, **kwargs)(f)
        subdomain_route = app.route(
            path, subdomain="<library_short_name>", *args, **kwargs
        )(prefix_route)
        default_library_route = app.route(path, *args, **kwargs)(subdomain_route)
        return default_library_route

    return decorator


def library_dir_route(path, *args, **kwargs):
    """Decorator to create library routes that work with or without a
    trailing slash."""
    if path.endswith("/"):
        path_without_slash = path[:-1]
    else:
        path_without_slash = path

    def decorator(f):
        # By default, creating a route with a slash will make flask redirect
        # requests without the slash, even if that route also exists.
        # Setting strict_slashes to False disables this behavior.
        # This is important for CORS because the redirects are not processed
        # by the CORS decorator and won't be valid CORS responses.

        # Decorate f with four routes, with and without the slash, with a prefix or subdomain
        prefix_slash = app.route(
            "/<library_short_name>" + path_without_slash + "/",
            strict_slashes=False,
            *args,
            **kwargs,
        )(f)
        prefix_no_slash = app.route(
            "/<library_short_name>" + path_without_slash, *args, **kwargs
        )(prefix_slash)
        subdomain_slash = app.route(
            path_without_slash + "/",
            strict_slashes=False,
            subdomain="<library_short_name>",
            *args,
            **kwargs,
        )(prefix_no_slash)
        subdomain_no_slash = app.route(
            path_without_slash, subdomain="<library_short_name>", *args, **kwargs
        )(subdomain_slash)
        default_library_slash = app.route(path_without_slash, *args, **kwargs)(
            subdomain_no_slash
        )
        default_library_no_slash = app.route(path_without_slash + "/", *args, **kwargs)(
            default_library_slash
        )
        return default_library_no_slash

    return decorator


@library_route("/", strict_slashes=False)
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def index():
    return app.manager.index_controller()


@library_route("/authentication_document")
@has_library
@returns_problem_detail
@compressible
def authentication_document():
    return app.manager.index_controller.authentication_document()


@library_route("/public_key_document")
@returns_problem_detail
@compressible
def public_key_document():
    return app.manager.index_controller.public_key_document()


@library_dir_route("/groups", defaults=dict(lane_identifier=None))
@library_route("/groups/<lane_identifier>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def acquisition_groups(lane_identifier):
    return app.manager.opds_feeds.groups(lane_identifier)


@library_route("/feed/qa/series")
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def qa_series_feed():
    return app.manager.opds_feeds.qa_series_feed()


@library_route("/feed/qa")
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def qa_feed():
    return app.manager.opds_feeds.qa_feed()


@library_dir_route("/feed", defaults=dict(lane_identifier=None))
@library_route("/feed/<lane_identifier>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def feed(lane_identifier):
    return app.manager.opds_feeds.feed(lane_identifier)


@library_dir_route("/navigation", defaults=dict(lane_identifier=None))
@library_route("/navigation/<lane_identifier>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def navigation_feed(lane_identifier):
    return app.manager.opds_feeds.navigation(lane_identifier)


@library_route("/crawlable")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def crawlable_library_feed():
    return app.manager.opds_feeds.crawlable_library_feed()


@library_route("/lists/<list_name>/crawlable")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def crawlable_list_feed(list_name):
    return app.manager.opds_feeds.crawlable_list_feed(list_name)


@app.route("/collections/<collection_name>/crawlable")
@allows_patron_web
@returns_problem_detail
@compressible
def crawlable_collection_feed(collection_name):
    return app.manager.opds_feeds.crawlable_collection_feed(collection_name)


@app.route("/collections/<collection_name>")
@returns_problem_detail
def shared_collection_info(collection_name):
    return app.manager.shared_collection_controller.info(collection_name)


@app.route("/collections/<collection_name>/register", methods=["POST"])
@returns_problem_detail
def shared_collection_register(collection_name):
    return app.manager.shared_collection_controller.register(collection_name)


@app.route(
    "/collections/<collection_name>/<identifier_type>/<path:identifier>/borrow",
    methods=["GET", "POST"],
    defaults=dict(hold_id=None),
)
@app.route(
    "/collections/<collection_name>/holds/<hold_id>/borrow",
    methods=["GET", "POST"],
    defaults=dict(identifier_type=None, identifier=None),
)
@returns_problem_detail
def shared_collection_borrow(collection_name, identifier_type, identifier, hold_id):
    return app.manager.shared_collection_controller.borrow(
        collection_name, identifier_type, identifier, hold_id
    )


@app.route("/collections/<collection_name>/loans/<loan_id>")
@returns_problem_detail
def shared_collection_loan_info(collection_name, loan_id):
    return app.manager.shared_collection_controller.loan_info(collection_name, loan_id)


@app.route("/collections/<collection_name>/loans/<loan_id>/revoke")
@returns_problem_detail
def shared_collection_revoke_loan(collection_name, loan_id):
    return app.manager.shared_collection_controller.revoke_loan(
        collection_name, loan_id
    )


@app.route(
    "/collections/<collection_name>/loans/<loan_id>/fulfill",
    defaults=dict(mechanism_id=None),
)
@app.route("/collections/<collection_name>/loans/<loan_id>/fulfill/<mechanism_id>")
@returns_problem_detail
def shared_collection_fulfill(collection_name, loan_id, mechanism_id):
    return app.manager.shared_collection_controller.fulfill(
        collection_name, loan_id, mechanism_id
    )


@app.route("/collections/<collection_name>/holds/<hold_id>")
@returns_problem_detail
def shared_collection_hold_info(collection_name, hold_id):
    return app.manager.shared_collection_controller.hold_info(collection_name, hold_id)


@app.route("/collections/<collection_name>/holds/<hold_id>/revoke")
@returns_problem_detail
def shared_collection_revoke_hold(collection_name, hold_id):
    return app.manager.shared_collection_controller.revoke_hold(
        collection_name, hold_id
    )


@library_route("/marc")
@has_library
@returns_problem_detail
@compressible
def marc_page():
    return app.manager.marc_records.download_page()


@library_dir_route("/search", defaults=dict(lane_identifier=None))
@library_route("/search/<lane_identifier>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def lane_search(lane_identifier):
    return app.manager.opds_feeds.search(lane_identifier)


@library_dir_route("/patrons/me", methods=["GET", "PUT"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def patron_profile():
    return app.manager.profiles.protocol()


@library_dir_route("/patrons/me/devices", methods=["GET"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def patron_devices():
    return app.manager.patron_devices.get_patron_device()


@library_dir_route("/patrons/me/devices", methods=["PUT"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def put_patron_devices():
    return app.manager.patron_devices.create_patron_device()


@library_dir_route("/loans", methods=["GET", "HEAD"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def active_loans():
    return app.manager.loans.sync()


@library_route("/annotations/", methods=["HEAD", "GET", "POST"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def annotations():
    return app.manager.annotations.container()


@library_route("/annotations/<annotation_id>", methods=["HEAD", "GET", "DELETE"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def annotation_detail(annotation_id):
    return app.manager.annotations.detail(annotation_id)


@library_route("/annotations/<identifier_type>/<path:identifier>", methods=["GET"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
@compressible
def annotations_for_work(identifier_type, identifier):
    return app.manager.annotations.container_for_work(identifier_type, identifier)


@library_route(
    "/works/<identifier_type>/<path:identifier>/borrow", methods=["GET", "PUT"]
)
@library_route(
    "/works/<identifier_type>/<path:identifier>/borrow/<mechanism_id>",
    methods=["GET", "PUT"],
)
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def borrow(identifier_type, identifier, mechanism_id=None):
    return app.manager.loans.borrow(identifier_type, identifier, mechanism_id)


@library_route("/works/<license_pool_id>/fulfill")
@library_route("/works/<license_pool_id>/fulfill/<mechanism_id>")
@library_route("/works/<license_pool_id>/fulfill/<mechanism_id>/<part>")
@has_library
@allows_patron_web
@returns_problem_detail
def fulfill(license_pool_id, mechanism_id=None, part=None):
    return app.manager.loans.fulfill(license_pool_id, mechanism_id, part)


@library_route("/loans/<license_pool_id>/revoke", methods=["GET", "PUT"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def revoke_loan_or_hold(license_pool_id):
    return app.manager.loans.revoke(license_pool_id)


@library_route("/loans/<identifier_type>/<path:identifier>", methods=["GET", "DELETE"])
@has_library
@allows_patron_web
@requires_auth
@returns_problem_detail
def loan_or_hold_detail(identifier_type, identifier):
    return app.manager.loans.detail(identifier_type, identifier)


@library_dir_route("/works")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def work():
    return app.manager.urn_lookup.work_lookup("work")


@library_dir_route(
    "/works/contributor/<contributor_name>",
    defaults=dict(languages=None, audiences=None),
)
@library_dir_route(
    "/works/contributor/<contributor_name>/<languages>", defaults=dict(audiences=None)
)
@library_route("/works/contributor/<contributor_name>/<languages>/<audiences>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def contributor(contributor_name, languages, audiences):
    return app.manager.work_controller.contributor(
        contributor_name, languages, audiences
    )


@library_dir_route(
    "/works/series/<series_name>", defaults=dict(languages=None, audiences=None)
)
@library_dir_route(
    "/works/series/<series_name>/<languages>", defaults=dict(audiences=None)
)
@library_route("/works/series/<series_name>/<languages>/<audiences>")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def series(series_name, languages, audiences):
    return app.manager.work_controller.series(series_name, languages, audiences)


@library_route("/works/<identifier_type>/<path:identifier>")
@has_library
@allows_auth
@allows_patron_web
@returns_problem_detail
@compressible
def permalink(identifier_type, identifier):
    return app.manager.work_controller.permalink(identifier_type, identifier)


@library_route("/works/<identifier_type>/<path:identifier>/recommendations")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def recommendations(identifier_type, identifier):
    return app.manager.work_controller.recommendations(identifier_type, identifier)


@library_route("/works/<identifier_type>/<path:identifier>/related_books")
@has_library
@allows_patron_web
@returns_problem_detail
@compressible
def related_books(identifier_type, identifier):
    return app.manager.work_controller.related(identifier_type, identifier)


@library_route(
    "/works/<identifier_type>/<path:identifier>/report", methods=["GET", "POST"]
)
@has_library
@allows_patron_web
@returns_problem_detail
def report(identifier_type, identifier):
    return app.manager.work_controller.report(identifier_type, identifier)


@library_route("/analytics/<identifier_type>/<path:identifier>/<event_type>")
@has_library
@allows_auth
@allows_patron_web
@returns_problem_detail
def track_analytics_event(identifier_type, identifier, event_type):
    return app.manager.analytics_controller.track_event(
        identifier_type, identifier, event_type
    )


# Adobe Vendor ID implementation
@library_route("/AdobeAuth/authdata")
@has_library
@requires_auth
@returns_problem_detail
def adobe_vendor_id_get_token():
    if not app.manager.adobe_vendor_id:
        return REMOTE_INTEGRATION_FAILED.detailed(
            _("This server does not have an Adobe Vendor ID server configured.")
        )
    return app.manager.adobe_vendor_id.create_authdata_handler(flask.request.patron)


@library_route("/AdobeAuth/SignIn", methods=["POST"])
@has_library
@returns_problem_detail
def adobe_vendor_id_signin():
    return app.manager.adobe_vendor_id.signin_handler()


@app.route("/AdobeAuth/AccountInfo", methods=["POST"])
@returns_problem_detail
def adobe_vendor_id_accountinfo():
    return app.manager.adobe_vendor_id.userinfo_handler()


@app.route("/AdobeAuth/Status")
@returns_problem_detail
def adobe_vendor_id_status():
    return app.manager.adobe_vendor_id.status_handler()


# DRM Device Management Protocol implementation for ACS.
@library_route("/AdobeAuth/devices", methods=["GET", "POST"])
@has_library
@requires_auth
@returns_problem_detail
def adobe_drm_devices():
    return app.manager.adobe_device_management.device_id_list_handler()


@library_route("/AdobeAuth/devices/<device_id>", methods=["DELETE"])
@has_library
@requires_auth
@returns_problem_detail
def adobe_drm_device(device_id):
    return app.manager.adobe_device_management.device_id_handler(device_id)


# Route that redirects to the authentication URL for an OAuth provider
@library_route("/oauth_authenticate")
@has_library
@returns_problem_detail
def oauth_authenticate():
    return app.manager.oauth_controller.oauth_authentication_redirect(
        flask.request.args, app.manager._db
    )


# Redirect URI for OAuth providers, eg. Clever
@library_route("/oauth_callback")
@has_library
@returns_problem_detail
def oauth_callback():
    return app.manager.oauth_controller.oauth_authentication_callback(
        app.manager._db, flask.request.args
    )


# Route that redirects to the authentication URL for a SAML provider
@library_route("/saml_authenticate")
@has_library
@returns_problem_detail
def saml_authenticate():
    return app.manager.saml_controller.saml_authentication_redirect(
        flask.request.args, app.manager._db
    )


# Redirect URI for SAML providers
# NOTE: we cannot use @has_library decorator and append a library's name to saml_calback route
# (e.g. https://cm.org/LIBRARY_NAME/saml_callback).
# The URL of the SP's assertion consumer service (saml_callback) should be constant:
# SP's metadata is registered in the IdP and cannot change.
# If we try to append a library's name to the ACS's URL sent as a part of the SAML request,
# the IdP will fail this request because the URL mentioned in the request and
# the URL saved in the SP's metadata configured in this IdP will differ.
# Library's name is passed as a part of the relay state and processed in SAMLController.saml_authentication_callback
@returns_problem_detail
@app.route("/saml_callback", methods=["POST"])
def saml_callback():
    return app.manager.saml_controller.saml_authentication_callback(
        request, app.manager._db
    )


@app.route("/<collection_name>/lcp/licenses/<license_id>/hint")
@app.route("/<collection_name>/lcp/licenses/<path:license_id>/hint")
@has_library_through_external_loan_identifier(parameter_name="license_id")
@requires_auth
@returns_problem_detail
def lcp_passphrase(collection_name, license_id):
    return app.manager.lcp_controller.get_lcp_passphrase()


@app.route("/<collection_name>/lcp/licenses/<license_id>")
@has_library_through_external_loan_identifier(parameter_name="license_id")
@requires_auth
@returns_problem_detail
def lcp_license(collection_name, license_id):
    return app.manager.lcp_controller.get_lcp_license(collection_name, license_id)


# Loan notifications for ODL distributors, eg. Feedbooks
@library_route("/odl_notify/<loan_id>", methods=["GET", "POST"])
@has_library
@returns_problem_detail
def odl_notify(loan_id):
    return app.manager.odl_notification_controller.notify(loan_id)


# Controllers used for operations purposes
@app.route("/heartbeat")
@returns_problem_detail
def heartbeat():
    return app.manager.heartbeat.heartbeat()


@app.route("/healthcheck.html")
def health_check():
    return Response("", 200)


@app.route("/images/<filename>")
def static_image(filename):
    return app.manager.static_files.image(filename)
