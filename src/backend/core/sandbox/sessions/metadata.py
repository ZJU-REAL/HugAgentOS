"""Product-specific sandbox ownership contract shared with the controller.

The controller image copies this dependency-free module as sandbox_metadata.
Keep the existing label value when porting a product; changing it invalidates
ownership checks for that product's running sandboxes.
"""

USER_ID_METADATA_KEY = "hugagent-user-id"
