# Copyright (c) 2024 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
from __future__ import annotations

import asyncio
from functools import cached_property
from logging import getLogger
from typing import TYPE_CHECKING

from ansible_collections.arista.avd.plugins.plugin_utils.cv_client.client import CVClient
from ansible_collections.arista.avd.plugins.plugin_utils.cv_client.workflows.models import CVDevice
from ansible_collections.arista.avd.plugins.plugin_utils.cv_client.workflows.verify_devices_on_cv import verify_devices_in_cloudvision_inventory
from ansible_collections.arista.avd.plugins.plugin_utils.eos_designs_shared_utils import SharedUtils
from ansible_collections.arista.avd.plugins.plugin_utils.errors import AristaAvdError
from ansible_collections.arista.avd.plugins.plugin_utils.utils import get

if TYPE_CHECKING:
    from ansible_collections.arista.avd.plugins.plugin_utils.cv_client.api.arista.swg.v1 import Location, VpnEndpoint

LOGGER = getLogger(__name__)


class UtilsZscalerMixin:
    """
    Mixin Class with internal functions.
    Class should only be used as Mixin to a AvdStructuredConfig class
    """

    # Set type hints for Attributes of the main class as needed
    _hostvars: dict
    shared_utils: SharedUtils

    @cached_property
    def _zscaler_endpoints(self) -> dict:
        """
        Returns zscaler_endpoints data model built via CloudVision API calls, unless they are provided in the input variables.

        Should only be called for CV Pathfinder Client devices.
        """
        zscaler_endpoints = get(self._hostvars, "zscaler_endpoints")
        if zscaler_endpoints is not None:
            return zscaler_endpoints

        return asyncio.run(self._generate_zscaler_endpoints()) or {}

    async def _generate_zscaler_endpoints(self):
        """
        Call CloudVision SWG APIs to generate the zscaler_endpoints model.

        Should only be called for CV Pathfinder Client devices.

        TODO: Add support for cv_verify_certs
        """
        context = "The WAN Internet-exit integration with Zscaler fetches information from CloudVision"
        cv_server = get(self._hostvars, "cv_server", required=True, org_key=f"{context} and requires 'cv_server' to be set.")
        cv_token = get(self._hostvars, "cv_token", required=True, org_key=f"{context} and requires 'cv_server' to be set.")
        wan_site_location = get(
            self.shared_utils.wan_site,
            "location",
            required=True,
            org_key=(
                f"{context} and requires 'cv_pathfinder_regions[name={self.shared_utils.wan_region['name']}]"
                f".sites[name={self.shared_utils.wan_site['name']}].location' to be set."
            ),
        )

        async with CVClient(servers=[cv_server], token=cv_token) as cv_client:
            cv_device = CVDevice(self.shared_utils.hostname, self.shared_utils.serial_number, self.shared_utils.system_mac_address)
            cv_inventory_devices: list[CVDevice] = await verify_devices_in_cloudvision_inventory(
                devices=[cv_device], skip_missing_devices=True, warnings=[], cv_client=cv_client
            )
            if not cv_inventory_devices:
                raise AristaAvdError(f"{context} but could not find '{self.shared_utils.hostname}' on the server '{cv_server}'.")
            if len(cv_inventory_devices) > 1:
                raise AristaAvdError(
                    (
                        f"{context} but found more than one device named '{self.shared_utils.hostname}' on the server '{cv_server}'. "
                        "Set 'serial_number' for the device in AVD vars, to ensure a unique match."
                    )
                )
            device_id: str = cv_inventory_devices[0].serial_number
            await cv_client.set_swg_device(device_id=device_id, service="zscaler", location=wan_site_location)
            cv_endpoint_status = await cv_client.wait_for_swg_endpoint_status(device_id=device_id, service="zscaler")

        device_location: Location = cv_endpoint_status.device_location

        zscaler_endpoints = {
            "cloud_name": cv_endpoint_status.cloud_name,
            "device_location": {
                "city": device_location.city,
                "country": device_location.country,
            },
        }
        if not getattr(cv_endpoint_status, "vpn_endpoints", None) or not getattr(cv_endpoint_status.vpn_endpoints, "values", None):
            raise AristaAvdError(f"{context} but did not get any IPsec Tunnel endpoints back from the Zscaler API.")

        for key in ("primary", "secondary", "tertiary"):
            if key in cv_endpoint_status.vpn_endpoints.values:
                vpn_endpoint: VpnEndpoint = cv_endpoint_status.vpn_endpoints.values[key]
                location: Location = vpn_endpoint.endpoint_location
                zscaler_endpoints[key] = {
                    "ip_address": vpn_endpoint.ip_address.value,
                    "datacenter": vpn_endpoint.datacenter,
                    "city": location.city,
                    "country": location.country,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                }

        return zscaler_endpoints