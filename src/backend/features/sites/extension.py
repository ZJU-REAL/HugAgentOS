"""Site design tool contribution; hosting routes retain central ordering."""
NAME = "sites"


def register_tools():
    from features.sites.tools.design_picker import register_native_contribution
    register_native_contribution()


def setup():
    register_tools()
    return True


def register_gateway_contributions():
    from core.harness.registry.transfer_channels import UploadChannel, UploadReceiver, register_upload_channel, register_result_localizer
    from features.sites import desktop_publish
    register_upload_channel(
        "sites", "publish_site",
        lambda: UploadChannel("site-publish", "application/gzip", desktop_publish.package_local_site),
        receiver=UploadReceiver(
            desktop_publish.SiteUploadOptions, desktop_publish.MAX_ARCHIVE_BYTES,
            lambda *args: desktop_publish.publish_uploaded_site(*args),
        ),
    )
    for name, method in {
        "publish_site": "localize_site_result",
        "manage_application": "localize_application_result",
        "publish_mcp": "localize_application_result",
    }.items():
        register_result_localizer(
            "sites", name,
            lambda *args, _method=method, **kwargs: getattr(desktop_publish, _method)(*args, **kwargs),
        )
