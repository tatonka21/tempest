# Copyright 2013 OpenStack Foundation
# Copyright 2013 IBM Corp
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import io

from oslo_log import log as logging
from tempest.api.image import base
from tempest.common import waiters
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.lib.common.utils import test_utils
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
import secrets

CONF = config.CONF
LOG = logging.getLogger(__name__)


class ImportImagesTest(base.BaseV2ImageTest):
    """Here we test the import operations for image"""

    @classmethod
    def skip_checks(cls):
        super(ImportImagesTest, cls).skip_checks()
        if not CONF.image_feature_enabled.import_image:
            skip_msg = (
                "%s skipped as image import is not available" % cls.__name__)
            raise cls.skipException(skip_msg)

    @classmethod
    def resource_setup(cls):
        super(ImportImagesTest, cls).resource_setup()
        cls.available_import_methods = cls.client.info_import()[
            'import-methods']['value']
        if not cls.available_import_methods:
            raise cls.skipException('Server does not support '
                                    'any import method')

    def _create_image(self, disk_format=None, container_format=None):
        # Create image
        uuid = '00000000-1111-2222-3333-444455556666'
        image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='image')
        container_format = container_format or CONF.image.container_formats[0]
        disk_format = disk_format or CONF.image.disk_formats[0]
        image = self.create_image(name=image_name,
                                  container_format=container_format,
                                  disk_format=disk_format,
                                  visibility='private',
                                  ramdisk_id=uuid)
        self.assertIn('name', image)
        self.assertEqual(image_name, image['name'])
        self.assertIn('visibility', image)
        self.assertEqual('private', image['visibility'])
        self.assertIn('status', image)
        self.assertEqual('queued', image['status'])
        return image

    def _require_import_method(self, method):
        if method not in self.available_import_methods:
            raise self.skipException('Server does not support '
                                     '%s import method' % method)

    def _stage_and_check(self):
        image = self._create_image()
        # Stage image data
        file_content = data_utils.random_bytes()
        image_file = io.BytesIO(file_content)
        self.client.stage_image_file(image['id'], image_file)
        # Check image status is 'uploading'
        body = self.client.show_image(image['id'])
        self.assertEqual(image['id'], body['id'])
        self.assertEqual('uploading', body['status'])
        return image['id']

    @decorators.idempotent_id('32ca0c20-e16f-44ac-8590-07869c9b4cc2')
    def test_image_glance_direct_import(self):
        """Test 'glance-direct' import functionalities

        Create image, stage image data, import image and verify
        that import succeeded.
        """
        self._require_import_method('glance-direct')

        image_id = self._stage_and_check()
        # import image from staging to backend
        resp = self.client.image_import(image_id, method='glance-direct')
        waiters.wait_for_image_imported_to_stores(self.client, image_id)

        if not self.versions_client.has_version('2.12'):
            # API is not new enough to support image/tasks API
            LOG.info('Glance does not support v2.12, so I am unable to '
                     'validate the image/tasks API.')
            return

        tasks = waiters.wait_for_image_tasks_status(
            self.client, image_id, 'success')
        self.assertEqual(1, len(tasks))
        task = tasks[0]
        self.assertEqual(resp.response['x-openstack-request-id'],
                         task['request_id'])
        self.assertEqual('glance-direct',
                         task['input']['import_req']['method']['name'])

    @decorators.idempotent_id('f6feb7a4-b04f-4706-a011-206129f83e62')
    def test_image_web_download_import(self):
        """Test 'web-download' import functionalities

        Create image, import image and verify that import
        succeeded.
        """
        self._require_import_method('web-download')

        image = self._create_image()
        # Now try to get image details
        body = self.client.show_image(image['id'])
        self.assertEqual(image['id'], body['id'])
        self.assertEqual('queued', body['status'])
        # import image from web to backend
        image_uri = CONF.image.http_image
        self.client.image_import(image['id'], method='web-download',
                                 import_params={'uri': image_uri})
        waiters.wait_for_image_imported_to_stores(self.client, image['id'])

    @decorators.idempotent_id('8876c818-c40e-4b90-9742-31d231616305')
    def test_image_glance_download_import_success(self):
        # We use glance-direct initially, then glance-download for test
        self._require_import_method('glance-direct')
        self._require_import_method('glance-download')

        # Create an image via the normal import process to be our source
        src = self._stage_and_check()
        self.client.image_import(src, method='glance-direct')
        waiters.wait_for_image_imported_to_stores(self.client, src)

        # Add some properties to it that will be copied by the default
        # config (and one that won't)
        self.client.update_image(src, [
            {'add': '/hw_cpu_cores', 'value': '5'},
            {'add': '/trait:STORAGE_DISK_SSD', 'value': 'required'},
            {'add': '/os_distro', 'value': 'rhel'},
            {'add': '/speed', 'value': '88mph'},
        ])

        # Make sure our properties stuck on the source image
        src_image = self.client.show_image(src)
        self.assertEqual('5', src_image['hw_cpu_cores'])
        self.assertEqual('required', src_image['trait:STORAGE_DISK_SSD'])
        self.assertEqual('rhel', src_image['os_distro'])
        self.assertEqual('88mph', src_image['speed'])

        # Create a new image which we will fill from another glance image
        dst = self._create_image(container_format='ovf',
                                 disk_format='iso')['id']

        # Set some values that will conflict to make sure we get the
        # new ones and confirm they stuck before the import.
        self.client.update_image(dst, [
            {'add': '/hw_cpu_cores', 'value': '1'},
            {'add': '/os_distro', 'value': 'windows'},
        ])
        dst_image = self.client.show_image(dst)
        self.assertEqual('1', dst_image['hw_cpu_cores'])
        self.assertEqual('windows', dst_image['os_distro'])

        params = {
            'glance_image_id': src,
            'glance_region': self.client.region,
            'glance_service_interface': 'public',
        }
        self.client.image_import(dst, method='glance-download',
                                 import_params=params)
        waiters.wait_for_image_tasks_status(self.client, dst, 'success')

        # Make sure the new image has all the keys imported from the
        # original image that we expect
        dst_image = self.client.show_image(dst)
        self.assertEqual(src_image['disk_format'], dst_image['disk_format'])
        self.assertEqual(src_image['container_format'],
                         dst_image['container_format'])
        self.assertEqual('5', dst_image['hw_cpu_cores'])
        self.assertEqual('required', dst_image['trait:STORAGE_DISK_SSD'])
        self.assertEqual('rhel', dst_image['os_distro'])
        self.assertNotIn('speed', dst_image)

    @decorators.attr(type=['negative'])
    @decorators.idempotent_id('36d4b546-64a2-4bb9-bdd0-ba676aa48f2c')
    def test_image_glance_download_import_bad_uuid(self):
        self._require_import_method('glance-download')
        image_id = self._create_image()['id']
        params = {
            'glance_image_id': 'foo',
            'glance_region': self.client.region,
            'glance_service_interface': 'public',
        }

        # A non-UUID-like image id should make us fail immediately
        e = self.assertRaises(lib_exc.BadRequest,
                              self.client.image_import,
                              image_id, method='glance-download',
                              import_params=params)
        self.assertIn('image id does not look like a UUID', str(e))

    @decorators.attr(type=['negative'])
    @decorators.idempotent_id('77644240-dbbe-4744-ae28-09b2ac12e218')
    def test_image_glance_download_import_bad_endpoint(self):
        self._require_import_method('glance-download')
        image_id = self._create_image()['id']

        # Set some properties before the import to make sure they are
        # undisturbed
        self.client.update_image(image_id, [
            {'add': '/hw_cpu_cores', 'value': '1'},
            {'add': '/os_distro', 'value': 'windows'},
        ])
        image = self.client.show_image(image_id)
        self.assertEqual('1', image['hw_cpu_cores'])
        self.assertEqual('windows', image['os_distro'])

        params = {
            'glance_image_id': '36d4b546-64a2-4bb9-bdd0-ba676aa48f2c',
            'glance_region': 'not a region',
            'glance_service_interface': 'not an interface',
        }

        # A bad region or interface will cause us to fail when we
        # contact the remote glance.
        self.client.image_import(image_id, method='glance-download',
                                 import_params=params)
        waiters.wait_for_image_tasks_status(self.client, image_id, 'failure')

        # Make sure we reverted the image status to queued on failure, and that
        # our extra properties are still in place.
        image = self.client.show_image(image_id)
        self.assertEqual('queued', image['status'])
        self.assertEqual('1', image['hw_cpu_cores'])
        self.assertEqual('windows', image['os_distro'])

    @decorators.attr(type=['negative'])
    @decorators.idempotent_id('c7edec8e-24b5-416a-9d42-b3e773bab62c')
    def test_image_glance_download_import_bad_missing_image(self):
        self._require_import_method('glance-download')
        image_id = self._create_image()['id']
        params = {
            'glance_image_id': '36d4b546-64a2-4bb9-bdd0-ba676aa48f2c',
            'glance_region': self.client.region,
            'glance_service_interface': 'public',
        }

        # A non-existent image will cause us to fail when we
        # contact the remote glance.
        self.client.image_import(image_id, method='glance-download',
                                 import_params=params)
        waiters.wait_for_image_tasks_status(self.client, image_id, 'failure')

        # Make sure we reverted the image status to queued on failure
        image = self.client.show_image(image_id)
        self.assertEqual('queued', image['status'])

    @decorators.idempotent_id('e04761a1-22af-42c2-b8bc-a34a3f12b585')
    def test_remote_import(self):
        """Test image import against a different worker than stage.

        This creates and stages an image against the primary API worker,
        but then calls import on a secondary worker (if available) to
        test that distributed image import works (i.e. proxies the import
        request to the proper worker).
        """
        self._require_import_method('glance-direct')

        if not CONF.image.alternate_image_endpoint:
            raise self.skipException('No image_remote service to test '
                                     'against')

        image_id = self._stage_and_check()
        # import image from staging to backend, but on the alternate worker
        self.os_primary.image_client_remote.image_import(
            image_id, method='glance-direct')
        waiters.wait_for_image_imported_to_stores(self.client, image_id)

    @decorators.idempotent_id('44d60544-1524-42f7-8899-315301105dd8')
    def test_remote_delete(self):
        """Test image delete against a different worker than stage.

        This creates and stages an image against the primary API worker,
        but then calls delete on a secondary worker (if available) to
        test that distributed image import works (i.e. proxies the delete
        request to the proper worker).
        """
        self._require_import_method('glance-direct')

        if not CONF.image.alternate_image_endpoint:
            raise self.skipException('No image_remote service to test '
                                     'against')

        image_id = self._stage_and_check()
        # delete image from staging to backend, but on the alternate worker
        self.os_primary.image_client_remote.delete_image(image_id)
        self.client.wait_for_resource_deletion(image_id)


class MultiStoresImportImagesTest(base.BaseV2ImageTest):
    """Test importing image in multiple stores"""
    @classmethod
    def skip_checks(cls):
        super(MultiStoresImportImagesTest, cls).skip_checks()
        if not CONF.image_feature_enabled.import_image:
            skip_msg = (
                "%s skipped as image import is not available" % cls.__name__)
            raise cls.skipException(skip_msg)

    @classmethod
    def resource_setup(cls):
        super(MultiStoresImportImagesTest, cls).resource_setup()
        cls.available_import_methods = cls.client.info_import()[
            'import-methods']['value']
        if not cls.available_import_methods:
            raise cls.skipException('Server does not support '
                                    'any import method')

        # NOTE(pdeore): Skip if glance-direct import method and mutlistore
        # are not enabled/configured, or only one store is configured in
        # multiple stores setup.
        cls.available_stores = cls.get_available_stores()
        if ('glance-direct' not in cls.available_import_methods or
                not len(cls.available_stores) > 1):
            raise cls.skipException(
                'Either glance-direct import method not present in %s or '
                'None or only one store is '
                'configured %s' % (cls.available_import_methods,
                                   cls.available_stores))

    @decorators.idempotent_id('bf04ff00-3182-47cb-833a-f1c6767b47fd')
    def test_glance_direct_import_image_to_all_stores(self):
        """Test image is imported in all available stores

        Create image, import image to all available stores using glance-direct
        import method and verify that import succeeded.
        """
        image, stores = self.create_and_stage_image(all_stores=True)

        self.client.image_import(
            image['id'], method='glance-direct', all_stores=True)

        waiters.wait_for_image_imported_to_stores(self.client,
                                                  image['id'], stores)

    @decorators.idempotent_id('82fb131a-dd2b-11ea-aec7-340286b6c574')
    def test_glance_direct_import_image_to_specific_stores(self):
        """Test image is imported in all available stores

        Create image, import image to specified store(s) using glance-direct
        import method and verify that import succeeded.
        """
        image, stores = self.create_and_stage_image()
        self.client.image_import(image['id'], method='glance-direct',
                                 stores=stores)

        waiters.wait_for_image_imported_to_stores(self.client, image['id'],
                                                  (','.join(stores)))


class BasicOperationsImagesTest(base.BaseV2ImageTest):
    """Here we test the basic operations of images"""

    @decorators.attr(type='smoke')
    @decorators.idempotent_id('139b765e-7f3d-4b3d-8b37-3ca3876ee318')
    def test_register_upload_get_image_file(self):
        """Here we test these functionalities

        Register image, upload the image file, get image and get image
        file api's
        """

        uuid = '00000000-1111-2222-3333-444455556666'
        image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='image')
        container_format = CONF.image.container_formats[0]
        disk_format = CONF.image.disk_formats[0]
        image = self.create_image(name=image_name,
                                  container_format=container_format,
                                  disk_format=disk_format,
                                  visibility='private',
                                  ramdisk_id=uuid)
        self.assertIn('name', image)
        self.assertEqual(image_name, image['name'])
        self.assertIn('visibility', image)
        self.assertEqual('private', image['visibility'])
        self.assertIn('status', image)
        self.assertEqual('queued', image['status'])

        # NOTE: This Glance API returns different status codes for image
        # condition. In this empty data case, Glance should return 204,
        # so here should check the status code.
        image_file = self.client.show_image_file(image['id'])
        self.assertEqual(0, len(image_file.data))
        self.assertEqual(204, image_file.response.status)

        # Now try uploading an image file
        file_content = data_utils.random_bytes()
        image_file = io.BytesIO(file_content)
        self.client.store_image_file(image['id'], image_file)

        # Now try to get image details
        body = self.client.show_image(image['id'])
        self.assertEqual(image['id'], body['id'])
        self.assertEqual(image_name, body['name'])
        self.assertEqual(uuid, body['ramdisk_id'])
        self.assertIn('size', body)
        self.assertEqual(1024, body.get('size'))

        # Now try get image file
        # NOTE: This Glance API returns different status codes for image
        # condition. In this non-empty data case, Glance should return 200,
        # so here should check the status code.
        body = self.client.show_image_file(image['id'])
        self.assertEqual(file_content, body.data)
        self.assertEqual(200, body.response.status)

    @decorators.attr(type='smoke')
    @decorators.idempotent_id('f848bb94-1c6e-45a4-8726-39e3a5b23535')
    def test_delete_image(self):
        """Test deleting an image by image_id"""
        # Create image
        image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='image')
        container_format = CONF.image.container_formats[0]
        disk_format = CONF.image.disk_formats[0]
        image = self.create_image(name=image_name,
                                  container_format=container_format,
                                  disk_format=disk_format,
                                  visibility='private')
        # Delete Image
        self.client.delete_image(image['id'])
        self.client.wait_for_resource_deletion(image['id'])

        # Verifying deletion
        images = self.client.list_images()['images']
        images_id = [item['id'] for item in images]
        self.assertNotIn(image['id'], images_id)

    @decorators.attr(type='smoke')
    @decorators.idempotent_id('f66891a7-a35c-41a8-b590-a065c2a1caa6')
    def test_update_image(self):
        """Test updating an image by image_id"""
        # Create image
        image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='image')
        container_format = CONF.image.container_formats[0]
        disk_format = CONF.image.disk_formats[0]
        image = self.create_image(name=image_name,
                                  container_format=container_format,
                                  disk_format=disk_format,
                                  visibility='private')
        self.assertEqual('queued', image['status'])

        # Update Image
        new_image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='new-image')
        self.client.update_image(image['id'], [
            dict(replace='/name', value=new_image_name)])

        # Verifying updating

        body = self.client.show_image(image['id'])
        self.assertEqual(image['id'], body['id'])
        self.assertEqual(new_image_name, body['name'])

    @decorators.idempotent_id('951ebe01-969f-4ea9-9898-8a3f1f442ab0')
    def test_deactivate_reactivate_image(self):
        """Test deactivating and reactivating an image"""
        # Create image
        image_name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix, name='image')
        image = self.create_image(name=image_name,
                                  container_format='bare',
                                  disk_format='raw',
                                  visibility='private')

        # Upload an image file
        content = data_utils.random_bytes()
        image_file = io.BytesIO(content)
        self.client.store_image_file(image['id'], image_file)

        # Deactivate image
        self.client.deactivate_image(image['id'])
        body = self.client.show_image(image['id'])
        self.assertEqual("deactivated", body['status'])

        # User unable to download deactivated image
        self.assertRaises(lib_exc.Forbidden, self.client.show_image_file,
                          image['id'])

        # Reactivate image
        self.client.reactivate_image(image['id'])
        body = self.client.show_image(image['id'])
        self.assertEqual("active", body['status'])

        # User able to download image after reactivation
        body = self.client.show_image_file(image['id'])
        self.assertEqual(content, body.data)


class ListUserImagesTest(base.BaseV2ImageTest):
    """Here we test the listing of image information"""

    @classmethod
    def resource_setup(cls):
        super(ListUserImagesTest, cls).resource_setup()
        # We add a few images here to test the listing functionality of
        # the images API
        container_fmts = CONF.image.container_formats
        disk_fmts = CONF.image.disk_formats
        all_pairs = [(container_fmt, disk_fmt)
                     for container_fmt in container_fmts
                     for disk_fmt in disk_fmts]

        for (container_fmt, disk_fmt) in all_pairs[:6]:
            LOG.debug("Creating an image "
                      "(Container format: %s, Disk format: %s).",
                      container_fmt, disk_fmt)
            cls._create_standard_image(container_fmt, disk_fmt)

    @classmethod
    def _create_standard_image(cls, container_format, disk_format):
        """Create a new standard image and return the newly-registered image-id

        Note that the size of the new image is a random number between
        1024 and 4096
        """
        size = secrets.SystemRandom().randint(1024, 4096)
        image_file = io.BytesIO(data_utils.random_bytes(size))
        prefix = CONF.resource_name_prefix
        tags = [data_utils.rand_name(prefix=prefix, name='tag'),
                data_utils.rand_name(prefix=prefix, name='tag')]
        image = cls.create_image(container_format=container_format,
                                 disk_format=disk_format,
                                 visibility='private',
                                 tags=tags)
        cls.client.store_image_file(image['id'], data=image_file)
        # Keep the data of one test image so it can be used to filter lists
        cls.test_data = image

        return image['id']

    def _list_by_param_value_and_assert(self, params):
        """Perform list action with given params and validates result."""
        # Retrieve the list of images that meet the filter
        images_list = self.client.list_images(params=params)['images']
        # Validating params of fetched images
        msg = 'No images were found that met the filter criteria.'
        self.assertNotEmpty(images_list, msg)
        for image in images_list:
            for key in params:
                msg = "Failed to list images by %s" % key
                self.assertEqual(params[key], image[key], msg)

    def _list_sorted_by_image_size_and_assert(self, params, desc=False):
        """Validate an image list that has been sorted by size

        Perform list action with given params and validates the results are
        sorted by image size in either ascending or descending order.
        """
        # Retrieve the list of images that meet the filter
        images_list = self.client.list_images(params=params)['images']
        # Validate that the list was fetched sorted accordingly
        msg = 'No images were found that met the filter criteria.'
        self.assertNotEmpty(images_list, msg)
        sorted_list = [image['size'] for image in images_list
                       if image['size'] is not None]
        msg = 'The list of images was not sorted correctly.'
        self.assertEqual(sorted(sorted_list, reverse=desc), sorted_list, msg)

    @decorators.idempotent_id('1e341d7a-90a9-494c-b143-2cdf2aeb6aee')
    def test_list_no_params(self):
        """Simple test to see all fixture images returned"""
        images_list = self.client.list_images()['images']
        image_list = [image['id'] for image in images_list]

        for image in self.created_images:
            self.assertIn(image, image_list)

    @decorators.idempotent_id('9959ca1d-1aa7-4b7a-a1ea-0fff0499b37e')
    def test_list_images_param_container_format(self):
        """Test to get all images with a specific container_format"""
        params = {"container_format": self.test_data['container_format']}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('4a4735a7-f22f-49b6-b0d9-66e1ef7453eb')
    def test_list_images_param_disk_format(self):
        """Test to get all images with disk_format = raw"""
        params = {"disk_format": "raw"}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('7a95bb92-d99e-4b12-9718-7bc6ab73e6d2')
    def test_list_images_param_visibility(self):
        """Test to get all images with visibility = private"""
        params = {"visibility": "private"}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('cf1b9a48-8340-480e-af7b-fe7e17690876')
    def test_list_images_param_size(self):
        """Test to get all images by size"""
        image_id = self.created_images[0]
        # Get image metadata
        image = self.client.show_image(image_id)

        params = {"size": image['size']}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('4ad8c157-971a-4ba8-aa84-ed61154b1e7f')
    def test_list_images_param_min_max_size(self):
        """Test to get all images with min size and max size"""
        image_id = self.created_images[0]
        # Get image metadata
        image = self.client.show_image(image_id)

        size = image['size']
        params = {"size_min": size - 500, "size_max": size + 500}
        images_list = self.client.list_images(params=params)['images']
        image_size_list = map(lambda x: x['size'], images_list)

        for image_size in image_size_list:
            self.assertGreaterEqual(image_size, params['size_min'],
                                    "Failed to get images by size_min")
            self.assertLessEqual(image_size, params['size_max'],
                                 "Failed to get images by size_max")

    @decorators.idempotent_id('7fc9e369-0f58-4d05-9aa5-0969e2d59d15')
    def test_list_images_param_status(self):
        """Test to get all active images"""
        params = {"status": "active"}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('e914a891-3cc8-4b40-ad32-e0a39ffbddbb')
    def test_list_images_param_limit(self):
        """Test to get images by limit"""
        params = {"limit": 1}
        images_list = self.client.list_images(params=params)['images']

        self.assertEqual(len(images_list), params['limit'],
                         "Failed to get images by limit")

    @decorators.idempotent_id('e9a44b91-31c8-4b40-a332-e0a39ffb4dbb')
    def test_list_image_param_owner(self):
        """Test to get images by owner"""
        image_id = self.created_images[0]
        # Get image metadata
        image = self.client.show_image(image_id)

        params = {"owner": image['owner']}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('55c8f5f5-bfed-409d-a6d5-4caeda985d7b')
    def test_list_images_param_name(self):
        """Test to get images by name"""
        params = {'name': self.test_data['name']}
        self._list_by_param_value_and_assert(params)

    @decorators.idempotent_id('aa8ac4df-cff9-418b-8d0f-dd9c67b072c9')
    def test_list_images_param_tag(self):
        """Test to get images matching a tag"""
        params = {'tag': self.test_data['tags'][0]}
        images_list = self.client.list_images(params=params)['images']
        # Validating properties of fetched images
        self.assertNotEmpty(images_list)
        for image in images_list:
            msg = ("The image {image_name} does not have the expected tag "
                   "{expected_tag} among its tags: {observerd_tags}."
                   .format(image_name=image['name'],
                           expected_tag=self.test_data['tags'][0],
                           observerd_tags=image['tags']))
            self.assertIn(self.test_data['tags'][0], image['tags'], msg)

    @decorators.idempotent_id('eeadce49-04e0-43b7-aec7-52535d903e7a')
    def test_list_images_param_sort(self):
        """Test listing images sorting in descending order"""
        params = {'sort': 'size:desc'}
        self._list_sorted_by_image_size_and_assert(params, desc=True)

    @decorators.idempotent_id('9faaa0c2-c3a5-43e1-8f61-61c54b409a49')
    def test_list_images_param_sort_key_dir(self):
        """Test listing images sorting by size in descending order"""
        params = {'sort_key': 'size', 'sort_dir': 'desc'}
        self._list_sorted_by_image_size_and_assert(params, desc=True)

    @decorators.idempotent_id('622b925c-479f-4736-860d-adeaf13bc371')
    def test_get_image_schema(self):
        """Test to get image schema"""
        schema = "image"
        body = self.schemas_client.show_schema(schema)
        self.assertEqual("image", body['name'])

    @decorators.idempotent_id('25c8d7b2-df21-460f-87ac-93130bcdc684')
    def test_get_images_schema(self):
        """Test to get images schema"""
        schema = "images"
        body = self.schemas_client.show_schema(schema)
        self.assertEqual("images", body['name'])

    @decorators.idempotent_id('d43f3efc-da4c-4af9-b636-868f0c6acedb')
    def test_list_hidden_image(self):
        image = self.client.create_image(os_hidden=True)
        image = image['image'] if 'image' in image else image
        self.addCleanup(self.client.wait_for_resource_deletion, image['id'])
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        self.client.delete_image, image['id'])
        images_list = self.client.list_images()['images']
        fetched_images_id = [img['id'] for img in images_list]
        self.assertNotIn(image['id'], fetched_images_id)

    @decorators.idempotent_id('fdb96b81-257b-42ac-978b-ddeefa3760e4')
    def test_list_update_hidden_image(self):
        image = self.create_image()
        images_list = self.client.list_images()['images']
        fetched_images_id = [img['id'] for img in images_list]
        self.assertIn(image['id'], fetched_images_id)

        self.client.update_image(image['id'],
                                 [dict(replace='/os_hidden', value=True)])
        images_list = self.client.list_images()['images']
        fetched_images_id = [img['id'] for img in images_list]
        self.assertNotIn(image['id'], fetched_images_id)


class ListSharedImagesTest(base.BaseV2ImageTest):
    """Here we test the listing of a shared image information"""

    credentials = ['primary', 'alt']

    @classmethod
    def setup_clients(cls):
        super(ListSharedImagesTest, cls).setup_clients()
        cls.image_member_client = cls.os_primary.image_member_client_v2
        cls.alt_img_client = cls.os_alt.image_client_v2

    @decorators.idempotent_id('3fa50be4-8e38-4c02-a8db-7811bb780122')
    def test_list_images_param_member_status(self):
        """Test listing images by member_status and visibility"""
        # Create an image to be shared using default visibility
        image_file = io.BytesIO(data_utils.random_bytes(2048))
        container_format = CONF.image.container_formats[0]
        disk_format = CONF.image.disk_formats[0]
        image = self.create_image(container_format=container_format,
                                  disk_format=disk_format)
        self.client.store_image_file(image['id'], data=image_file)

        # Share the image created with the alt user
        self.image_member_client.create_image_member(
            image_id=image['id'], member=self.alt_img_client.tenant_id)

        # As an image consumer you need to provide the member_status parameter
        # along with the visibility=shared parameter in order for it to show
        # results
        params = {'member_status': 'pending', 'visibility': 'shared'}
        fetched_images = self.alt_img_client.list_images(params)['images']
        self.assertEqual(1, len(fetched_images))
        self.assertEqual(image['id'], fetched_images[0]['id'])


class ImageLocationsTest(base.BaseV2ImageTest):
    @classmethod
    def skip_checks(cls):
        super(ImageLocationsTest, cls).skip_checks()
        if not CONF.image_feature_enabled.manage_locations:
            skip_msg = (
                "%s skipped as show_multiple_locations is not available" % (
                    cls.__name__))
            raise cls.skipException(skip_msg)

    @decorators.idempotent_id('58b0fadc-219d-40e1-b159-1c902cec323a')
    def test_location_after_upload(self):
        image = self.client.create_image(container_format='bare',
                                         disk_format='raw')

        # Locations should be empty when there is no data
        self.assertEqual('queued', image['status'])
        self.assertEqual([], image['locations'])

        # Now try uploading an image file
        file_content = data_utils.random_bytes()
        image_file = io.BytesIO(file_content)
        self.client.store_image_file(image['id'], image_file)
        waiters.wait_for_image_status(self.client, image['id'], 'active')

        # Locations should now have one item
        image = self.client.show_image(image['id'])
        self.assertEqual(1, len(image['locations']),
                         'Expected one location in %r' % image['locations'])

        # NOTE(danms): If show_image_direct_url is enabled, then this
        # will be present. If so, it should match the one location we set
        if 'direct_url' in image:
            self.assertEqual(image['direct_url'], image['locations'][0]['url'])

        return image

    @decorators.idempotent_id('37599b8a-d5c0-4590-aee5-73878502be15')
    def test_set_location(self):
        self.check_set_location()

    @decorators.idempotent_id('bf6e0009-c039-4884-b498-db074caadb10')
    def test_replace_location(self):
        image = self.check_set_multiple_locations()
        original_locs = image['locations']

        # Replacing with the exact thing should work
        self.client.update_image(image['id'], [
            dict(replace='/locations', value=image['locations'])])

        # Changing metadata on a location should work
        original_locs[0]['metadata']['date'] = '2015-10-15'
        self.client.update_image(image['id'], [
            dict(replace='/locations', value=original_locs)])

        # Deleting a location should not work
        self.assertRaises(
            lib_exc.BadRequest,
            self.client.update_image,
            image['id'], [
                dict(replace='/locations', value=[original_locs[0]])])

        # Replacing a location (with a different URL) should not work
        new_loc = {'metadata': original_locs[1]['metadata'],
                   'url': '%s#new3' % CONF.image.http_image}
        self.assertRaises(
            lib_exc.BadRequest,
            self.client.update_image,
            image['id'], [
                dict(replace='/locations', value=[original_locs[0],
                                                  new_loc])])

        # Make sure the locations haven't changed with the above failures,
        # but the metadata we updated should be changed.
        image = self.client.show_image(image['id'])
        self.assertEqual(2, len(image['locations']),
                         'Image should have two locations but has %i' % (
                         len(image['locations'])))
        self.assertEqual(original_locs, image['locations'])

    @decorators.idempotent_id('a9a20396-8399-4b36-909d-564949be098f')
    def test_set_location_bad_scheme(self):
        image = self.client.create_image(container_format='bare',
                                         disk_format='raw')

        # Locations should be empty when there is no data
        self.assertEqual('queued', image['status'])
        self.assertEqual([], image['locations'])

        # Adding a new location using a scheme that is not allowed
        # should result in an error
        new_loc = {'metadata': {'foo': 'bar'},
                   'url': 'gopher://info.cern.ch'}
        self.assertRaises(lib_exc.BadRequest,
                          self.client.update_image, image['id'], [
                              dict(add='/locations/-', value=new_loc)])

    def _check_set_location_with_hash(self):
        image = self.client.create_image(container_format='bare',
                                         disk_format='raw')

        # Create a new location with validation data
        new_loc = {'validation_data': {'checksum': '1' * 32,
                                       'os_hash_value': 'deadbeef' * 16,
                                       'os_hash_algo': 'sha512'},
                   'metadata': {},
                   'url': CONF.image.http_image}
        self._update_image_with_retries(image['id'],
                                        [dict(add='/locations/-',
                                              value=new_loc)])

        # Expect that all of our values ended up on the image
        image = self.client.show_image(image['id'])
        self.assertEqual(1, len(image['locations']))
        self.assertEqual('1' * 32, image['checksum'])
        self.assertEqual('deadbeef' * 16, image['os_hash_value'])
        self.assertEqual('sha512', image['os_hash_algo'])
        self.assertNotIn('validation_data', image['locations'][0])
        self.assertEqual('active', image['status'])

        return image

    @decorators.idempotent_id('42d6f7db-c9f5-4bae-9e15-a90262fe445a')
    def test_set_location_with_hash(self):
        self._check_set_location_with_hash()

    @decorators.idempotent_id('304c8a19-aa86-47dd-a022-ec4c7f433f1b')
    def test_set_location_with_hash_second_matching(self):
        orig_image = self._check_set_location_with_hash()

        new_loc = {
            'validation_data': {'checksum': orig_image['checksum'],
                                'os_hash_value': orig_image['os_hash_value'],
                                'os_hash_algo': orig_image['os_hash_algo']},
            'metadata': {},
            'url': '%s#new' % CONF.image.http_image}
        self._update_image_with_retries(orig_image['id'],
                                        [dict(add='/locations/-',
                                              value=new_loc)])

        # Setting the same exact values on a new location should work
        image = self.client.show_image(orig_image['id'])
        self.assertEqual(2, len(image['locations']))
        self.assertEqual(orig_image['checksum'], image['checksum'])
        self.assertEqual(orig_image['os_hash_value'], image['os_hash_value'])
        self.assertEqual(orig_image['os_hash_algo'], image['os_hash_algo'])
        self.assertNotIn('validation_data', image['locations'][0])
        self.assertNotIn('validation_data', image['locations'][1])

    @decorators.idempotent_id('f3ce99c2-9ffb-4b9f-b2cb-876929382553')
    def test_set_location_with_hash_not_matching(self):
        orig_image = self._check_set_location_with_hash()
        values = {
            'checksum': '2' * 32,
            'os_hash_value': 'beefdead' * 16,
            'os_hash_algo': 'sha256',
        }

        # Try to set a new location with one each of the above
        # substitutions
        for k, v in values.items():
            new_loc = {
                'validation_data': {
                    'checksum': orig_image['checksum'],
                    'os_hash_value': orig_image['os_hash_value'],
                    'os_hash_algo': orig_image['os_hash_algo']},
                'metadata': {},
                'url': '%s#new' % CONF.image.http_image}
            new_loc['validation_data'][k] = v

            # This should always fail due to the mismatch
            self.assertRaises(lib_exc.Conflict,
                              self._update_image_with_retries,
                              orig_image['id'],
                              [dict(add='/locations/-', value=new_loc)])

        # Now try to add a new location with all of the substitutions,
        # which should also fail
        new_loc['validation_data'] = values
        self.assertRaises(lib_exc.Conflict,
                          self._update_image_with_retries,
                          orig_image['id'],
                          [dict(add='/locations/-', value=new_loc)])

        # Make sure nothing has changed on our image after all the
        # above failures
        image = self.client.show_image(orig_image['id'])
        self.assertEqual(1, len(image['locations']))
        self.assertEqual(orig_image['checksum'], image['checksum'])
        self.assertEqual(orig_image['os_hash_value'], image['os_hash_value'])
        self.assertEqual(orig_image['os_hash_algo'], image['os_hash_algo'])
        self.assertNotIn('validation_data', image['locations'][0])
