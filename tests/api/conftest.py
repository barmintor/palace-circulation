from pytest import register_assert_rewrite

register_assert_rewrite("tests.fixtures.database")
register_assert_rewrite("tests.fixtures.files")
register_assert_rewrite("tests.fixtures.vendor_id")

pytest_plugins = [
    "core.testing",
    "tests.fixtures.announcements",
    "tests.fixtures.api_axis_files",
    "tests.fixtures.api_bibliotheca_files",
    "tests.fixtures.api_config",
    "tests.fixtures.api_controller",
    "tests.fixtures.api_enki_files",
    "tests.fixtures.api_feedbooks_files",
    "tests.fixtures.api_images_files",
    "tests.fixtures.api_kansas_files",
    "tests.fixtures.api_millenium_files",
    "tests.fixtures.api_novelist_files",
    "tests.fixtures.api_nyt_files",
    "tests.fixtures.api_odilo_files",
    "tests.fixtures.api_odl2_files",
    "tests.fixtures.api_odl_files",
    "tests.fixtures.api_onix_files",
    "tests.fixtures.api_opds_dist_files",
    "tests.fixtures.api_opds_files",
    "tests.fixtures.api_overdrive_files",
    "tests.fixtures.csv_files",
    "tests.fixtures.database",
    "tests.fixtures.files",
    "tests.fixtures.marc_files",
    "tests.fixtures.odl",
    "tests.fixtures.opds2_files",
    "tests.fixtures.opds_files",
    "tests.fixtures.overdrive",
    "tests.fixtures.s3",
    "tests.fixtures.sample_covers",
    "tests.fixtures.search",
    "tests.fixtures.time",
    "tests.fixtures.tls_server",
    "tests.fixtures.vendor_id",
]
