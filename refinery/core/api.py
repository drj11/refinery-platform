'''
Created on May 4, 2012

@author: nils
'''

import datetime
import json
import logging
import re
from sets import Set
import uuid

from django.conf import settings
from django.conf.urls.defaults import url
from django.contrib.auth.models import User, Group
from django.contrib.sites.models import get_current_site
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMessage
from django.template import loader, Context

from guardian.shortcuts import get_objects_for_user, get_objects_for_group, \
    get_perms
from tastypie import fields
from tastypie.authentication import SessionAuthentication, Authentication
from tastypie.authorization import Authorization
from tastypie.bundle import Bundle
from tastypie.constants import ALL_WITH_RELATIONS, ALL
from tastypie.exceptions import Unauthorized, ImmediateHttpResponse
from tastypie.http import HttpNotFound, HttpForbidden, HttpBadRequest, \
    HttpUnauthorized, HttpMethodNotAllowed, HttpAccepted, HttpCreated, \
    HttpNoContent, HttpGone
from tastypie.resources import ModelResource, Resource
from tastypie.utils import trailing_slash

from core.models import Project, NodeSet, NodeRelationship, NodePair, \
    Workflow, WorkflowInputRelationships, Analysis, DataSet, \
    ExternalToolStatus, ResourceStatistics, GroupManagement, ExtendedGroup, \
    UserAuthentication, Invitation, UserProfile, FastQC
from core.tasks import check_tool_status
from data_set_manager.api import StudyResource, AssayResource, \
    InvestigationResource
from data_set_manager.models import Node, Study, Attribute
from file_store.models import FileStoreItem

from fadapa import Fadapa

logger = logging.getLogger(__name__)


# Specifically made for descendants of SharableResource.
class SharableResourceAPIInterface(object):
    uuid_regex = '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'

    def __init__(self, res_type):
        self.res_type = res_type

    # Useful getter methods that process data.

    def get_res(self, res_uuid):
        res_list = self.res_type.objects.filter(uuid=res_uuid)
        return None if len(res_list) == 0 else res_list[0]

    def get_group(self, group_id):
        group_list = Group.objects.filter(id=int(group_id))
        return None if len(group_list) == 0 else group_list[0]

    def is_manager_group(self, group):
        return not group.extendedgroup.is_managed()

    def get_perms(self, res, group):
        # Default values.
        perms = {'read': False, 'change': False}

        # Find matching ones if available.
        for i in res.get_groups():
            if i['group'].group_ptr.id == group.id:
                perms = {'read': i['read'], 'change': i['change']}

        return perms

    def get_share_list(self, user, res):
        groups_in = filter(
            lambda g:
            not self.is_manager_group(g) and user in g.user_set.all(),
            Group.objects.all())

        return map(
            lambda g: {
                'group_id': g.id,
                'group_name': g.name,
                'perms': self.get_perms(res, g)},
            groups_in)

    def groups_with_user(self, user):
        return filter(lambda g: user in g.user_set.all(), Group.objects.all())

    # Generalizes bundle construction and resource processing. Turning on more
    # options may require going to the SharableResource class and adding them.

    # Apply filters.
    def query_filtering(self, res_list, get_req_dict):
        mod_list = res_list

        for k in get_req_dict:
            # Skip if res does not have the attribute. Done to help avoid
            # whatever internal filtering can be performed on other things,
            # like limiting the return amount.
            mod_list = [x for x in mod_list
                        if not hasattr(x, k) or
                        str(getattr(x, k)) == get_req_dict[k]]

        return mod_list

    def build_res_list(self, user):
        if user.is_authenticated():
            return get_objects_for_user(
                user,
                'core.read_%s' % self.res_type._meta.verbose_name
            )
        else:
            return get_objects_for_group(
                ExtendedGroup.objects.public_group(),
                'core.read_%s' % self.res_type._meta.verbose_name
            )

    # Turns on certain things depending on flags
    def transform_res_list(self, user, res_list, request, **kwargs):

        owned_res_set = Set(
            get_objects_for_user(
                user,
                'core.share_%s' %
                self.res_type._meta.verbose_name).values_list("id", flat=True))
        public_res_set = Set(
            get_objects_for_group(
                ExtendedGroup.objects.public_group(),
                'core.read_%s' %
                self.res_type._meta.verbose_name).values_list("id", flat=True))

        # instantiate owner and public fields
        for res in res_list:
            setattr(res, 'is_owner', res.id in owned_res_set)
            setattr(res, 'public', res.id in public_res_set)

            if 'sharing' in kwargs and kwargs['sharing']:
                setattr(res, 'share_list', self.get_share_list(user, res))

        # Filter for query flags.
        res_list = self.query_filtering(res_list, request.GET)

        return res_list

    def build_bundle_list(self, request, res_list, **kwargs):
        bundle_list = []

        for i in res_list:
            built_obj = self.build_bundle(obj=i, request=request)
            bundle_list.append(self.full_dehydrate(built_obj))

        return bundle_list

    # **kwargs added in case there is other data for future expansion.
    def build_object_list(self, bundle, **kwargs):
        return {
            'meta': {
                'total_count': len(bundle)
            },
            'objects': bundle
        }

    def build_response(self, request, object_list, **kwargs):
        return self.create_response(request, object_list)

    # Makes everything simpler for GET requests.
    def process_get(self, request, res, **kwargs):
        user = request.user
        mod_res_list = self.transform_res_list(user, [res], request, **kwargs)
        bundle = self.build_bundle_list(request, mod_res_list)[0]
        return self.build_response(request, bundle, **kwargs)

    def process_get_list(self, request, res_list, **kwargs):
        user = request.user
        mod_res_list = self.transform_res_list(
            user, res_list, request, **kwargs)
        bundle_list = self.build_bundle_list(request, mod_res_list, **kwargs)
        object_list = self.build_object_list(bundle_list, **kwargs)
        return self.build_response(request, object_list, **kwargs)

    # Overriding some ORM methods.

    # Handles POST requests.
    def obj_create(self, bundle, **kwargs):
        bundle = ModelResource.obj_create(self, bundle, **kwargs)
        bundle.obj.set_owner(bundle.request.user)
        return bundle

    # Some wacky custom job because ModelResource's get calls some things that
    # we don't want to get called :(
    def obj_get(self, bundle, **kwargs):
        res = self.get_res(kwargs['uuid'])
        request = bundle.request
        user = request.user

        if not res:
            return HttpBadRequest()

        # User not authenticated, res is not public.
        if not user.is_authenticated() and res and not res.is_public():
            return HttpUnauthorized()

        mod_res_list = self.transform_res_list(user, [res], request, **kwargs)
        bundle = self.build_bundle_list(request, mod_res_list)[0]
        return bundle.obj

    def obj_get_list(self, bundle, **kwargs):
        return self.get_object_list(bundle.request)

    def get_object_list(self, request):
        user = request.user
        obj_list = self.build_res_list(user)
        r_list = self.transform_res_list(user, obj_list, request)
        return r_list

    # A few default URL endpoints as directed by prepend_urls in subclasses.

    def prepend_urls(self):
        return [
            url(r'^(?P<resource_name>%s)/(?P<uuid>%s)/sharing/$' %
                (self._meta.resource_name, self.uuid_regex),
                self.wrap_view('res_sharing'),
                name='api_%s_sharing' % (self._meta.resource_name)),
            url(r'^(?P<resource_name>%s)/sharing/$' %
                (self._meta.resource_name),
                self.wrap_view('res_sharing_list'),
                name='api_%s_sharing_list' % (self._meta.resource_name)),
        ]

    # TODO: Make sure GuardianAuthorization works.
    def res_sharing(self, request, **kwargs):
        res = self.get_res(kwargs['uuid'])
        user = request.user

        if not res:
            return HttpBadRequest()

        if user.is_authenticated and user != res.get_owner():
            return HttpUnauthorized()

        # User not authenticated, res is not public.
        if not user.is_authenticated() and res and not res.is_public():
            return HttpUnauthorized()

        if request.method == 'GET':
            kwargs['sharing'] = True
            return self.process_get(request, res, **kwargs)
        elif request.method == 'PUT':
            data = json.loads(request.body)
            new_share_list = data['share_list']

            groups_shared_with = map(
                lambda g: g['group'].group_ptr,
                res.get_groups())

            # Unshare everything before sharing.
            for i in groups_shared_with:
                res.unshare(i)

            for i in new_share_list:
                group = self.get_group(int(i['id']))
                can_read = i['read']
                can_change = i['change']
                is_read_only = can_read and not can_change
                should_share = can_read or can_change

                if should_share:
                    res.share(group, is_read_only)

            return HttpAccepted()
        else:
            return HttpMethodNotAllowed()

    def res_sharing_list(self, request, **kwargs):
        if request.method == 'GET':
            kwargs['sharing'] = True
            res_list = self.build_res_list(request.user)
            return self.process_get_list(request, res_list, **kwargs)
        else:
            return HttpMethodNotAllowed()


class ProjectResource(ModelResource, SharableResourceAPIInterface):
    share_list = fields.ListField(attribute='share_list', null=True)
    public = fields.BooleanField(attribute='public', null=True)
    is_owner = fields.BooleanField(attribute='is_owner', null=True)

    def __init__(self):
        SharableResourceAPIInterface.__init__(self, Project)
        ModelResource.__init__(self)

    class Meta:
        # authentication = ApiKeyAuthentication()
        queryset = Project.objects.filter(is_catch_all=False)
        resource_name = 'projects'
        detail_uri_name = 'uuid'
        fields = ['name', 'id', 'uuid', 'summary']
        # authentication = SessionAuthentication()
        # authorization = GuardianAuthorization()
        authorization = Authorization()

    def prepend_urls(self):
        return SharableResourceAPIInterface.prepend_urls(self)

    def res_sharing_list(self, request, **kwargs):
        if request.method == 'GET':
            kwargs['sharing'] = True
            res_list = filter(
                lambda r: not r.is_catch_all,
                self.build_res_list(request.user)
            )
            return self.process_get_list(request, res_list, **kwargs)
        else:
            return HttpMethodNotAllowed()

    def obj_create(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_create(self, bundle, **kwargs)

    def obj_get(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get(self, bundle, **kwargs)

    def obj_get_list(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get_list(
            self, bundle, **kwargs)

    def get_object_list(self, request):
        obj_list = SharableResourceAPIInterface.get_object_list(self, request)
        return filter(lambda o: not o.is_catch_all, obj_list)


class DataSetResource(ModelResource, SharableResourceAPIInterface):
    uuid_regex = '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
    share_list = fields.ListField(attribute='share_list', null=True)
    public = fields.BooleanField(attribute='public', null=True)
    is_owner = fields.BooleanField(attribute='is_owner', null=True)

    def __init__(self):
        SharableResourceAPIInterface.__init__(self, DataSet)
        ModelResource.__init__(self)

    class Meta:
        queryset = DataSet.objects.all()
        detail_uri_name = 'uuid'  # for using UUIDs instead of pk in URIs
        # allowed_methods = ['get']
        resource_name = 'data_sets'
        # authentication = SessionAuthentication()
        # authorization = GuardianAuthorization()
        filtering = {
            'uuid': ALL,
            'is_owner': ALL,
            'public': ALL
        }

    def apply_sorting(self, obj_list, options=None):
        """Manually sorting the list of objects returned by
        `SharableResourceAPIInterface`. Ordering is being triggered in the same
        way it normally is.

        Example:
        # Ascending
        /api/v1/resource/?order_by=attribute
        # Descending
        /api/v1/resource/?order_by=-attribute
        """
        if options and 'order_by' in options:
            if options['order_by'][0] == '-':
                reverse = True
                sorting = options['order_by'][1:]
            else:
                reverse = False
                sorting = options['order_by']
        else:
            # Default sorting
            sorting = 'modification_date'
            reverse = True

        obj_list.sort(
            key=lambda x: getattr(x, sorting),
            reverse=reverse
        )

        return obj_list

    def prepend_urls(self):
        prepend_urls_list = SharableResourceAPIInterface.prepend_urls(self) + [
            url(r'^(?P<resource_name>%s)/(?P<uuid>%s)/investigation%s$' % (
                self._meta.resource_name,
                self.uuid_regex,
                trailing_slash()
            ),
                self.wrap_view('get_investigation'),
                name='api_%s_get_investigation' % (
                    self._meta.resource_name)
                ),
            url(r'^(?P<resource_name>%s)/(?P<uuid>%s)/studies%s$' % (
                self._meta.resource_name,
                self.uuid_regex,
                trailing_slash()
            ),
                self.wrap_view('get_studies'),
                name='api_%s_get_studies' % (
                    self._meta.resource_name
                )),
            url(r'^(?P<resource_name>%s)/(?P<uuid>%s)/analyses%s$' % (
                self._meta.resource_name,
                self.uuid_regex,
                trailing_slash()
            ),
                self.wrap_view('get_analyses'),
                name='api_%s_get_analyses' % (
                    self._meta.resource_name
                )),
            url(r'^(?P<resource_name>%s)/(?P<uuid>%s)/studies/'
                r'(?P<study_uuid>%s)/assays%s$' % (
                    self._meta.resource_name,
                    self.uuid_regex,
                    self.uuid_regex,
                    trailing_slash()
                ),
                self.wrap_view('get_assays'),
                name='api_%s_get_assays' % (
                    self._meta.resource_name
                )),
        ]
        return prepend_urls_list

    def obj_get(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get(self, bundle, **kwargs)

    def obj_get_list(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get_list(
            self, bundle, **kwargs)

    def get_object_list(self, request):
        obj_list = SharableResourceAPIInterface.get_object_list(self, request)
        return obj_list

    def obj_create(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_create(self, bundle, **kwargs)

    def get_investigation(self, request, **kwargs):
        try:
            data_set = DataSet.objects.get(uuid=kwargs['uuid'])
        except ObjectDoesNotExist:
            return HttpGone()

        # Assuming 1 to 1 relationship between DataSet and Investigation
        return InvestigationResource().get_detail(
            request,
            uuid=data_set.get_investigation().uuid
        )

    def get_studies(self, request, **kwargs):
        try:
            data_set = DataSet.objects.get(uuid=kwargs['uuid'])
        except ObjectDoesNotExist:
            return HttpGone()

        return StudyResource().get_list(
            request=request,
            investigation_uuid=data_set.get_investigation().uuid
        )

    def get_analyses(self, request, **kwargs):
        try:
            DataSet.objects.get(uuid=kwargs['uuid'])
        except ObjectDoesNotExist:
            return HttpGone()

        return AnalysisResource().get_list(
            request=request,
            data_set__uuid=kwargs['uuid']
        )

    def get_assays(self, request, **kwargs):
        try:
            DataSet.objects.get(uuid=kwargs['uuid'])
            Study.objects.get(uuid=kwargs['study_uuid'])
        except ObjectDoesNotExist:
            return HttpGone()

        return AssayResource().get_list(
            request=request,
            study_uuid=kwargs['study_uuid']
        )


class WorkflowResource(ModelResource, SharableResourceAPIInterface):
    input_relationships = fields.ToManyField(
        "core.api.WorkflowInputRelationshipsResource", 'input_relationships',
        full=True)
    share_list = fields.ListField(attribute='share_list', null=True)
    public = fields.BooleanField(attribute='public', null=True)
    is_owner = fields.BooleanField(attribute='is_owner', null=True)

    def __init__(self):
        SharableResourceAPIInterface.__init__(self, Workflow)
        ModelResource.__init__(self)

    class Meta:
        queryset = Workflow.objects.filter(is_active=True).order_by('name')
        detail_resource_name = 'workflow'
        resource_name = 'workflow'
        detail_uri_name = 'uuid'
        # allowed_methods = ['get']
        fields = ['name', 'uuid', 'summary']
        # authentication = SessionAuthentication()
        # authorization = GuardianAuthorization()

    def apply_sorting(self, obj_list, options=None):
        """Same as dataSet sorting
        """
        if options and 'order_by' in options:
            if options['order_by'][0] == '-':
                reverse = True
                sorting = options['order_by'][1:]
            else:
                reverse = False
                sorting = options['order_by']
        else:
            # Default sorting
            sorting = 'name'
            reverse = False

        obj_list.sort(
            key=lambda x: getattr(x, sorting),
            reverse=reverse
        )

        return obj_list

    def prepend_urls(self):
        return SharableResourceAPIInterface.prepend_urls(self)

    def obj_get(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get(self, bundle, **kwargs)

    def obj_get_list(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_get_list(
            self, bundle, **kwargs)

    def get_object_list(self, request):
        obj_list = SharableResourceAPIInterface.get_object_list(self, request)
        return filter(lambda o: o.is_active, obj_list)

    def obj_create(self, bundle, **kwargs):
        return SharableResourceAPIInterface.obj_create(self, bundle, **kwargs)

    def dehydrate(self, bundle):
        # detect if detail
        if self.get_resource_uri(bundle) == bundle.request.path:
            # detail detected, add graph as json
            try:
                bundle.data['graph'] = json.loads(bundle.obj.graph)
            except ValueError:
                logger.error(
                    "Failed to decode workflow graph into dictionary for " +
                    "workflow '%s'", str(bundle.obj))
                # don't include in response if error occurs
        bundle.data['author'] = bundle.obj.get_owner()
        bundle.data['galaxy_instance_identifier'] = \
            bundle.obj.workflow_engine.instance.api_key
        return bundle


class WorkflowInputRelationshipsResource(ModelResource):
    class Meta:
        queryset = WorkflowInputRelationships.objects.all()
        detail_resource_name = 'workflowrelationships'
        resource_name = 'workflowrelationships'
        # detail_uri_name = 'uuid'
        fields = ['category', 'set1', 'set2', 'workflow']


class AnalysisResource(ModelResource):
    data_set = fields.ToOneField(DataSetResource, 'data_set', use_in='detail')
    uuid = fields.CharField(attribute='uuid', use_in='all')
    name = fields.CharField(attribute='name', use_in='all')
    data_set__uuid = fields.CharField(attribute='data_set__uuid', use_in='all')
    workflow__uuid = fields.CharField(attribute='workflow__uuid', use_in='all')
    creation_date = fields.CharField(attribute='creation_date', use_in='all')
    modification_date = fields.CharField(
        attribute='modification_date',
        use_in='all'
    )
    workflow_steps_num = fields.IntegerField(
        attribute='workflow_steps_num', blank=True, null=True, use_in='detail')
    workflow_copy = fields.CharField(
        attribute='workflow_copy', blank=True, null=True, use_in='detail')
    history_id = fields.CharField(
        attribute='history_id', blank=True, null=True, use_in='detail')
    workflow_galaxy_id = fields.CharField(
        attribute='workflow_galaxy_id', blank=True, null=True, use_in='detail')
    library_id = fields.CharField(
        attribute='library_id', blank=True, null=True, use_in='detail')
    time_start = fields.DateTimeField(
        attribute='time_start', blank=True, null=True, use_in='detail')
    time_end = fields.DateTimeField(
        attribute='time_end', blank=True, null=True, use_in='detail')
    status = fields.CharField(
        attribute='status', default=Analysis.INITIALIZED_STATUS, blank=True,
        null=True, use_in='detail')

    class Meta:
        queryset = Analysis.objects.all()
        resource_name = Analysis._meta.module_name
        detail_uri_name = 'uuid'  # for using UUIDs instead of pk in URIs
        # required for public data set access by anonymous users
        authentication = Authentication()
        authorization = Authorization()
        allowed_methods = ["get"]
        fields = [
            'data_set', 'data_set__uuid', 'creation_date',
            'modification_date', 'history_id', 'library_id', 'name',
            'workflow__uuid', 'resource_uri', 'status', 'time_end',
            'time_start', 'uuid', 'workflow_galaxy_id', 'workflow_steps_num',
            'workflow_copy'
        ]
        filtering = {
            'data_set': ALL_WITH_RELATIONS,
            'workflow_steps_num': ALL_WITH_RELATIONS,
            'data_set__uuid': ALL,
            'status': ALL,
            'uuid': ALL
        }
        ordering = ['name', 'creation_date', 'time_start', 'time_end']

    def get_object_list(self, request, **kwargs):
        user = request.user
        perm = 'read_%s' % DataSet._meta.module_name
        if (user.is_authenticated()):
            allowed_datasets = get_objects_for_user(user, perm, DataSet)
        else:
            allowed_datasets = get_objects_for_group(
                ExtendedGroup.objects.public_group(), perm, DataSet)

        return Analysis.objects.filter(
            data_set__in=allowed_datasets.values_list("id", flat=True)
        )


class NodeResource(ModelResource):
    parents = fields.ToManyField('core.api.NodeResource', 'parents')
    study = fields.ToOneField('data_set_manager.api.StudyResource', 'study')
    assay = fields.ToOneField(
        'data_set_manager.api.AssayResource', 'assay', null=True
    )
    attributes = fields.ToManyField(
        'data_set_manager.api.AttributeResource',
        attribute=lambda bundle: (
            Attribute.objects
            .exclude(value__isnull=True)
            .exclude(value__exact='')
            .filter(
                node=bundle.obj,
                subtype='organism'
            )
        ),
        use_in='all',
        full=True,
        null=True
    )

    class Meta:
        queryset = Node.objects.all()
        resource_name = 'node'
        detail_uri_name = 'uuid'  # for using UUIDs instead of pk in URIs
        # required for public data set access by anonymous users
        authentication = Authentication()
        authorization = Authorization()
        allowed_methods = ["get"]
        fields = [
            'name', 'uuid', 'file_uuid', 'file_url', 'study', 'assay',
            'children', 'type', 'analysis_uuid', 'subanalysis', 'attributes'
        ]
        filtering = {
            'uuid': ALL,
            'study': ALL_WITH_RELATIONS,
            'assay': ALL_WITH_RELATIONS,
            'file_uuid': ALL,
            'type': ALL
        }
        limit = 0
        max_limit = 0

    def prepend_urls(self):
        return [
            url(r"^(?P<resource_name>%s)/(?P<uuid>"
                r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
                r")/$" %
                self._meta.resource_name,
                self.wrap_view('dispatch_detail'),
                name="api_dispatch_detail"),
        ]

    def dehydrate(self, bundle):
        # return download URL of file if a file is associated with the node
        try:
            file_item = FileStoreItem.objects.get(uuid=bundle.obj.file_uuid)
        except AttributeError:
            logger.warning("No UUID provided")
            bundle.data['file_url'] = None
            bundle.data['file_import_status'] = None
        except FileStoreItem.DoesNotExist:
            logger.warning(
                "Unable to find file store item with UUID '%s'",
                bundle.obj.file_uuid)
            bundle.data['file_url'] = None
            bundle.data['file_import_status'] = None
        else:
            bundle.data['file_url'] = file_item.get_full_url()
            bundle.data['file_import_status'] = file_item.get_import_status()
        return bundle

        # def get_object_list(self, request):
        #     """
        #     Temporarily removed for performance reasons (and not required
        #     without authorization)
        #     Get all nodes available to the current user (via dataset)
        #     Temp workaround due to Node being not Ownable
        #
        #     """
        #     user = request.user
        #     perm = 'read_%s' % DataSet._meta.module_name
        #     if (user.is_authenticated()):
        #         allowed_datasets = get_objects_for_user(user, perm, DataSet)
        #     else:
        #         allowed_datasets = get_objects_for_group(
        #             ExtendedGroup.objects.public_group(), perm, DataSet)
        #     # get list of node UUIDs that belong to all datasets available to
        #     # the current user
        #     all_allowed_studies = []
        #     for dataset in allowed_datasets:
        #         dataset_studies = dataset.get_investigation().study_set.all()
        #         all_allowed_studies.extend(
        #               [study for study in dataset_studies]
        #         )
        #     allowed_nodes = []
        #     for study in all_allowed_studies:
        #         allowed_nodes.extend(study.node_set.all().values('uuid'))
        #     # filter nodes using that list
        #     return super(NodeResource, self).get_object_list(request).filter(
        #         uuid__in=[node['uuid'] for node in allowed_nodes])


class NodeSetResource(ModelResource):
    # https://github.com/toastdriven/django-tastypie/pull/538
    # https://github.com/toastdriven/django-tastypie/issues/526
    # Once the above has been integrated into a tastypie release branch remove
    # NodeSetListResource and use "use_in" instead
    # nodes = fields.ToManyField(NodeResource, 'nodes', use_in="detail" )

    solr_query = fields.CharField(attribute='solr_query', null=True)
    solr_query_components = fields.CharField(
        attribute='solr_query_components', null=True)
    node_count = fields.IntegerField(attribute='node_count', null=True)
    is_implicit = fields.BooleanField(attribute='is_implicit')
    study = fields.ToOneField(StudyResource, 'study')
    assay = fields.ToOneField(AssayResource, 'assay')

    class Meta:
        # create node count attribute on the fly - node_count field has to be
        # defined on resource
        queryset = NodeSet.objects.all().order_by('-is_current', 'name')
        resource_name = 'nodeset'
        detail_uri_name = 'uuid'  # for using UUIDs instead of pk in URIs
        authentication = SessionAuthentication()
        authorization = Authorization()
        fields = [
            'is_current', 'name', 'summary', 'assay', 'study', 'uuid',
            'is_implicit', 'node_count', 'solr_query', 'solr_query_components'
        ]
        ordering = [
            'is_current', 'name', 'summary', 'assay', 'study', 'uuid',
            'is_implicit', 'node_count', 'solr_query', 'solr_query_components'
        ]
        allowed_methods = ["get", "post", "put"]
        filtering = {
            "study": ALL_WITH_RELATIONS, "assay": ALL_WITH_RELATIONS,
            "uuid": ALL
        }
        # jQuery treats a 201 as an error for data type "JSON"
        always_return_data = True

    def prepend_urls(self):
        return [
            url(r"^(?P<resource_name>%s)/(?P<uuid>"
                r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
                r")/$" %
                self._meta.resource_name,
                self.wrap_view('dispatch_detail'),
                name="api_dispatch_detail"),
        ]

    def obj_create(self, bundle, **kwargs):
        """Create a new NodeSet instance and assign current user as owner if
        current user has read permission on the data set referenced by the new
        NodeSet
        """
        # get the Study specified by the UUID in the new NodeSet
        study_uri = bundle.data['study']
        match = re.search(
            '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}',
            study_uri)
        study_uuid = match.group()
        try:
            study = Study.objects.get(uuid=study_uuid)
        except Study.DoesNotExist:
            logger.error("Study '{}' does not exist".format(study_uuid))
            self.unauthorized_result(
                Unauthorized("You are not allowed to create a new NodeSet."))
        # look up the dataset via InvestigationLink relationship
        # an investigation is only associated with a single dataset even though
        # InvestigationLink is a many to many relationship
        try:
            dataset = \
                study.investigation.investigationlink_set.all()[0].data_set
        except IndexError:
            logger.error("Data set not found in study '{}'".format(study.uuid))
            self.unauthorized_result(
                Unauthorized("You are not allowed to create a new NodeSet."))
        permission = "read_%s" % dataset._meta.module_name
        if not bundle.request.user.has_perm(permission, dataset):
            self.unauthorized_result(
                Unauthorized("You are not allowed to create a new NodeSet."))
        # if user has the read permission on the data set
        # continue with creating the new NodeSet instance
        bundle = super(NodeSetResource, self).obj_create(bundle, **kwargs)
        bundle.obj.set_owner(bundle.request.user)
        return bundle


class NodeSetListResource(ModelResource):
    study = fields.ToOneField(StudyResource, 'study')
    assay = fields.ToOneField(AssayResource, 'assay')
    node_count = fields.IntegerField(attribute='node_count', readonly=True)
    is_implicit = fields.BooleanField(attribute='is_implicit')

    class Meta:
        # create node count attribute on the fly - node_count field has to be
        # defined on resource
        queryset = NodeSet.objects.all().order_by('-is_current', 'name')
        # NG: introduced to get correct resource IDs
        detail_resource_name = 'nodeset'
        resource_name = 'nodesetlist'
        detail_uri_name = 'uuid'  # for using UUIDs instead of pk in URIs
        authentication = SessionAuthentication()
        authorization = Authorization()
        fields = ['is_current', 'name', 'summary', 'assay', 'study', 'uuid']
        allowed_methods = ["get"]
        filtering = {"study": ALL_WITH_RELATIONS, "assay": ALL_WITH_RELATIONS}
        ordering = ['is_current', 'name', 'node_count']

    def dehydrate(self, bundle):
        # replace resource URI to point to the nodeset resource instead of the
        # nodesetlist resource
        bundle.data['resource_uri'] = bundle.data['resource_uri'].replace(
            self._meta.resource_name, self._meta.detail_resource_name)
        return bundle


class NodePairResource(ModelResource):
    node1 = fields.ToOneField(NodeResource, 'node1')
    node2 = fields.ToOneField(NodeResource, 'node2', null=True)
    group = fields.CharField(attribute='group', null=True)

    class Meta:
        detail_allowed_methods = ['get', 'post', 'delete', 'put', 'patch']
        queryset = NodePair.objects.all()
        detail_resource_name = 'nodepair'
        resource_name = 'nodepair'
        detail_uri_name = 'uuid'
        authentication = SessionAuthentication()
        authorization = Authorization()
        # for use with AngularJS $resources: returns newly created object upon
        # POST (in addition to the location response header)
        always_return_data = True


class NodeRelationshipResource(ModelResource):
    name = fields.CharField(attribute='name', null=True)
    type = fields.CharField(attribute='type', null=True)
    # , full=True), if you need each attribute for each nodepair
    node_pairs = fields.ToManyField(NodePairResource, 'node_pairs')
    study = fields.ToOneField(StudyResource, 'study')
    assay = fields.ToOneField(AssayResource, 'assay')

    class Meta:
        detail_allowed_methods = ['get', 'post', 'delete', 'put', 'patch']
        queryset = NodeRelationship.objects.all().order_by('-is_current',
                                                           'name')
        detail_resource_name = 'noderelationship'
        resource_name = 'noderelationship'
        detail_uri_name = 'uuid'
        authentication = SessionAuthentication()
        authorization = Authorization()
        # for use with AngularJS $resources: returns newly created object upon
        # POST (in addition to the location response header)
        always_return_data = True
        # fields = ['type', 'study', 'assay', 'node_pairs']
        ordering = ['is_current', 'name', 'type', 'node_pairs']
        filtering = {'study': ALL_WITH_RELATIONS, 'assay': ALL_WITH_RELATIONS}


class ExternalToolStatusResource(ModelResource):
    class Meta:
        queryset = ExternalToolStatus.objects.all()
        resource_name = 'externaltoolstatus'
        authentication = Authentication()
        authorization = Authorization()
        allowed_methods = ["get"]
        fields = [
            'name',
            'is_active',
            'last_time_check',
            'unique_instance_identifier'
        ]

    def dehydrate(self, bundle):
        # call to method
        bundle.data['status'] = check_tool_status(bundle.data['name'])[1]
        return bundle


class StatisticsResource(Resource):
    user = fields.IntegerField(attribute='user')
    group = fields.IntegerField(attribute='group')
    files = fields.IntegerField(attribute='files')
    dataset = fields.DictField(attribute='dataset')
    workflow = fields.DictField(attribute='workflow')
    project = fields.DictField(attribute='project')

    def stat_summary(self, model):
        model_list = model.objects.all()
        total = len(model_list)

        public = len(filter(lambda x: x.is_public(), model_list))

        private_shared = len(filter(
            lambda x: not x.is_public() and len(x.get_groups()) > 1,
            model_list))

        private = total - public - private_shared
        return {
            'total': total, 'public': public,
            'private': private, 'private_shared': private_shared
        }

    class Meta:
        resource_name = 'statistics'
        object_class = ResourceStatistics

    def detail_uri_kwargs(self, bundle_or_obj):
        kwargs = {}
        kwargs['pk'] = uuid.uuid1()
        return kwargs

    def obj_get_list(self, bundle, **kwargs):
        return self.get_object_list(bundle.request)

    def get_object_list(self, request):
        user_count = User.objects.count()
        group_count = Group.objects.count()
        files_count = FileStoreItem.objects.count()
        dataset_summary = {}
        workflow_summary = {}
        project_summary = {}

        if 'dataset' in request.GET:
            dataset_summary = self.stat_summary(DataSet)
        if 'workflow' in request.GET:
            workflow_summary = self.stat_summary(Workflow)
        if 'project' in request.GET:
            project_summary = self.stat_summary(Project)

        request_string = request.GET.get('type')

        if request_string is not None:
            if 'dataset' in request_string:
                dataset_summary = self.stat_summary(DataSet)
            if 'workflow' in request_string:
                workflow_summary = self.stat_summary(Workflow)
            if 'project' in request_string:
                project_summary = self.stat_summary(Project)

        return [ResourceStatistics(
            user_count, group_count, files_count,
            dataset_summary, workflow_summary, project_summary)]


class GroupManagementResource(Resource):
    group_id = fields.IntegerField(attribute='group_id', null=True)
    group_name = fields.CharField(attribute='group_name', null=True)
    member_list = fields.ListField(attribute='member_list', null=True)
    perm_list = fields.ListField(attribute='perm_list', null=True)
    can_edit = fields.BooleanField(attribute='can_edit', default=False)
    manager_group = fields.BooleanField(attribute='is_manager_group',
                                        default=False)
    manager_group_id = fields.IntegerField(attribute='manager_group_id',
                                           null=True)

    class Meta:
        resource_name = 'groups'
        object_class = GroupManagement
        detail_uri_name = 'group_id'
        authentication = SessionAuthentication()
        # authorization = GuardianAuthorization
        authorization = Authorization()

    def get_user(self, user_id):
        user_list = User.objects.filter(id=int(user_id))
        return None if len(user_list) == 0 else user_list[0]

    def get_group(self, group_id):
        group_list = Group.objects.filter(id=int(group_id))
        return None if len(group_list) == 0 else group_list[0]

    def groups_with_user(self, user):
        # Allow or disallow manager groups to show in queries.
        return filter(
            lambda g:
            not self.is_manager_group(g) and user in g.user_set.all(),
            Group.objects.all())

    def is_manager_group(self, group):
        return not group.extendedgroup.is_managed()

    # Removes the user from both the manager and user group.
    def full_remove(self, user, group):
        if self.is_manager_group(group):
            group.user_set.remove(user)

            for i in group.extendedgroup.managed_group.all():
                i.user_set.remove(user)
        else:
            group.user_set.remove(user)
            group.extendedgroup.manager_group.user_set.remove(user)

    def get_member_list(self, group):
        return map(
            lambda u: {
                'user_id': u.id,
                'username': u.username,
                'first_name': u.first_name,
                'last_name': u.last_name,
                'is_manager': self.is_manager_group(group) and
                              u in group.user_set.all() or
                              not self.is_manager_group(group) and
                              u in
                              group.extendedgroup.manager_group.user_set.all()
            },
            group.user_set.all())

    # Group permissions against a single resource.
    def get_perms(self, res, group):
        # Default values.
        perms = {
            'uuid': res.uuid,
            'name': res.name,
            'type': res._meta.object_name,
            'read': False,
            'change': False
        }

        # Find matching perms if available.
        for i in res.get_groups():
            if i['group'].group_ptr.id == group.id:
                perms['read'] = i['read']
                perms['change'] = i['change']

        return perms

    def get_perm_list(self, group):
        dataset_perms = filter(
            lambda r: r['read'],
            map(lambda r: self.get_perms(r, group), DataSet.objects.all()))
        project_perms = filter(
            lambda r: r['read'],
            map(lambda r: self.get_perms(r, group), Project.objects.all()))
        workflow_perms = filter(
            lambda r: r['read'],
            map(lambda r: self.get_perms(r, group), Workflow.objects.all()))
        # workflow_engine_perms = map(f, WorkflowEngine.objects.all())
        # analysis_perms = map(f, Analysis.objects.all())
        # download_perms = map(f, Download.objects.all())
        return dataset_perms + project_perms + workflow_perms

    # Bundle building methods.

    # The group_list is actually a list of GroupManagement objects.
    def build_group_list(self, user, group_list, **kwargs):
        if 'members' in kwargs and kwargs['members']:
            for i in group_list:
                group = self.get_group(i.group_id)
                setattr(i, 'member_list', self.get_member_list(group))

        if 'perms' in kwargs and kwargs['perms']:
            for i in group_list:
                group = self.get_group(i.group_id)
                setattr(i, 'perm_list', self.get_perm_list(group))

        return group_list

    def build_bundle_list(self, request, group_list, **kwargs):
        bundle = []

        for i in group_list:
            built_obj = self.build_bundle(obj=i, request=request)
            bundle.append(self.full_dehydrate(built_obj))

        return bundle

    def build_object_list(self, bundle, **kwargs):
        return {
            'meta': {
                'total_count': len(bundle)
            },
            'objects': bundle
        }

    def build_response(self, request, object_list, **kwargs):
        return self.create_response(request, object_list)

    # Simplify things for GET requests.
    def process_get(self, request, group, **kwargs):
        user = request.user
        m_group_list = self.build_group_list(user, [group], **kwargs)
        bundle = self.build_bundle_list(request, m_group_list, **kwargs)[0]
        return self.build_response(request, bundle, **kwargs)

    def process_get_list(self, request, group_list, **kwargs):
        user = request.user
        m_group_list = self.build_group_list(user, group_list, **kwargs)
        bundle = self.build_bundle_list(request, m_group_list, **kwargs)
        object_list = self.build_object_list(bundle, **kwargs)
        return self.build_response(request, object_list, **kwargs)

    # This implies that users just have to be in the manager group, not
    # necessarily in the group itself.
    def user_authorized(self, user, group):
        if self.is_manager_group(group):
            return user in group.user_set.all()
        else:
            return user in group.extendedgroup.manager_group.user_set.all()

    # Endpoints for this resource.

    def detail_uri_kwargs(self, bundle_or_obj):
        kwargs = {}

        if isinstance(bundle_or_obj, Bundle):
            kwargs['group_id'] = bundle_or_obj.obj.group_id
        else:
            kwargs['group_id'] = bundle_or_obj.group_id

        return kwargs

    def prepend_urls(self):
        return [
            url(r'^groups/(?P<id>[0-9]+)/$',
                self.wrap_view('group_basic'),
                name='api_group_basic'),
            url(r'^groups/$',
                self.wrap_view('group_basic_list'),
                name='api_group_basic_list'),
            url(r'^groups/(?P<id>[0-9]+)/members/$',
                self.wrap_view('group_members'),
                name='api_group_members'),
            url(r'^groups/(?P<id>[0-9]+)/members/(?P<user_id>[0-9])/$',
                self.wrap_view('group_members_detail'),
                name='api_group_members_detail'),
            url(r'^groups/members/$',
                self.wrap_view('group_members_list'),
                name='api_group_members_list'),
            url(r'^groups/(?P<id>[0-9]+)/perms/$',
                self.wrap_view('group_perms'),
                name='api_group_perms'),
            url(r'^groups/perms/$',
                self.wrap_view('group_perms_list'),
                name='api_group_perms_list'),
        ]

    def group_basic(self, request, **kwargs):
        user = request.user
        group = self.get_group(kwargs['id'])

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            group_obj = GroupManagement(
                group.id,
                group.name,
                None,
                None,
                self.user_authorized(user, group),
                self.is_manager_group(group),
                group.id
                if self.is_manager_group(group)
                else group.extendedgroup.manager_group.id)
            return self.process_get(request, group_obj, **kwargs)
        elif request.method == 'DELETE':
            if not self.user_authorized(user, group):
                return HttpUnauthorized()

            # Cannot delete manager groups directly, must delete their managed
            # group, which causes manager group deletion.
            if self.is_manager_group(group):
                return HttpUnauthorized()

            group.delete()
            group.extendedgroup.manager_group.delete()
            return HttpNoContent()
        else:
            return HttpMethodNotAllowed()

    def group_basic_list(self, request, **kwargs):
        user = request.user

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            group_list = self.groups_with_user(user)

            group_obj_list = map(
                lambda g: GroupManagement(
                    g.id,
                    g.name,
                    None,
                    None,
                    self.user_authorized(user, g),
                    self.is_manager_group(g),
                    g.id
                    if self.is_manager_group(g)
                    else g.extendedgroup.manager_group.id),
                group_list)

            return self.process_get_list(request, group_obj_list, **kwargs)
        elif request.method == 'POST':
            data = json.loads(request.raw_post_data)
            new_group = ExtendedGroup(name=data['name'])
            new_group.save()
            new_group.group_ptr.user_set.add(user)
            new_group.manager_group.user_set.add(user)
            return HttpCreated()
        else:
            return HttpMethodNotAllowed()

    def group_members(self, request, **kwargs):
        user = request.user
        group = self.get_group(kwargs['id'])

        if request.method == 'GET':
            group_obj = GroupManagement(
                group.id,
                group.name,
                self.get_member_list(group),
                None,
                self.user_authorized(user, group),
                self.is_manager_group(group),
                group.id
                if self.is_manager_group(group)
                else group.extendedgroup.manager_group.id)
            kwargs['members'] = True
            return self.process_get(request, group_obj, **kwargs)
        elif request.method == 'PATCH':
            if not self.user_authorized(user, group):
                return HttpUnauthorized()

            data = json.loads(request.raw_post_data)
            new_member_list = data['member_list']

            # Remove old members before updating.
            group.user_set.clear()

            for m in new_member_list:
                group.user_set.add(int(m['id']))

            # Managers should also be in groups they manage.
            if self.is_manager_group(group):
                for g in group.extendedgroup.managed_group.all():
                    for m in new_member_list:
                        g.user_set.add(int(m['id']))

            return HttpAccepted()
        elif request.method == 'POST':
            if not self.user_authorized(user, group):
                return HttpUnauthorized()

            data = json.loads(request.raw_post_data)
            new_member = data['user_id']
            group.user_set.add(new_member)

            if self.is_manager_group(group):
                for g in group.extendedgroup.managed_group.all():
                    g.user_set.add(new_member)

            return HttpAccepted()
        else:
            return HttpMethodNotAllowed()

    def group_members_detail(self, request, **kwargs):
        group = self.get_group(kwargs['id'])
        user = request.user

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            raise NotImplementedError()
        elif request.method == 'DELETE':
            # Removing yourself - can't leave a group if you're the last
            # member, must delete it.
            if user.id == int(kwargs['user_id']):
                if group.user_set.count() == 1:
                    return HttpForbidden('Last member - must delete group')

                # When demoting yourself while targetting manager group.
                if (self.is_manager_group(group) and
                        group.user_set.count() == 1):
                    return HttpForbidden(
                        'Last manager must delete group to leave')

                if (not self.is_manager_group(group) and user in
                        group.extendedgroup.manager_group.user_set.all()):
                    if group.extendedgroup.manager_group.user_set.count() == 1:
                        return HttpForbidden(
                            'Last manager must delete group to leave')

                group.user_set.remove(user)

                if not self.is_manager_group(group):
                    group.extendedgroup.manager_group.user_set.remove(user)

                return HttpNoContent()

            # Removing other people from the group
            if not self.user_authorized(user, group):
                return HttpUnauthorized()

            if self.is_manager_group(group):
                group.user_set.remove(int(kwargs['user_id']))
            else:
                group.user_set.remove(int(kwargs['user_id']))
                group.extendedgroup.manager_group.user_set.remove(
                    int(kwargs['user_id']))
            return HttpNoContent()
        else:
            return HttpMethodNotAllowed()

    def group_members_list(self, request, **kwargs):
        user = request.user

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            group_list = self.groups_with_user(user)

            group_obj_list = map(
                lambda g: GroupManagement(
                    g.id,
                    g.name,
                    self.get_member_list(g),
                    None,
                    self.user_authorized(user, g),
                    self.is_manager_group(g),
                    g.id
                    if self.is_manager_group(g)
                    else g.extendedgroup.manager_group.id),
                group_list)

            kwargs['members'] = True
            return self.process_get_list(request, group_obj_list, **kwargs)
        else:
            return HttpMethodNotAllowed()

    def group_perms(self, request, **kwargs):
        user = request.user
        group = self.get_group(kwargs['id'])

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            group_obj = GroupManagement(
                group.id,
                group.name,
                None,
                self.get_perm_list(group),
                self.user_authorized(user, group),
                self.is_manager_group(group),
                group.id
                if self.is_manager_group(group)
                else group.extendedgroup.manager_group.id)
            kwargs['perms'] = True
            return self.process_get(request, group_obj, **kwargs)
        elif request.method == 'PATCH':
            raise NotImplementedError()
        else:
            return HttpMethodNotAllowed()

    def group_perms_list(self, request, **kwargs):
        user = request.user

        if not user.is_authenticated():
            return HttpUnauthorized()

        if request.method == 'GET':
            group_list = self.groups_with_user(user)

            group_obj_list = map(
                lambda g: GroupManagement(
                    g.id,
                    g.name,
                    None,
                    self.get_perm_list(g),
                    self.user_authorized(user, g),
                    self.is_manager_group(g),
                    g.id
                    if self.is_manager_group(g)
                    else g.extendedgroup.manager_group.id),
                group_list)

            kwargs['perms'] = True
            return self.process_get_list(request, group_obj_list, **kwargs)
        else:
            return HttpMethodNotAllowed()


class UserAuthenticationResource(Resource):
    is_logged_in = fields.BooleanField(attribute='is_logged_in', default=False)
    is_admin = fields.BooleanField(attribute='is_admin', default=False)
    id = fields.IntegerField(attribute='id', default=-1)
    username = fields.CharField(attribute='username', default='AnonymousUser')

    class Meta:
        resource_name = 'user_authentication'
        object_class = UserAuthentication

    def determine_format(self, request):
        return 'application/json'

    def prepend_urls(self):
        return [
            url(r'^user_authentication/$',
                self.wrap_view('check_user_status'),
                name='api_user_authentication_check'),
        ]

    def check_user_status(self, request, **kwargs):
        user = request.user
        is_logged_in = user.is_authenticated()
        is_admin = user.is_staff
        id = user.id
        username = user.username if is_logged_in else 'AnonymousUser'
        auth_obj = UserAuthentication(
            is_logged_in,
            is_admin,
            user.id,
            username
        )
        built_obj = self.build_bundle(obj=auth_obj, request=request)
        bundle = self.full_dehydrate(built_obj)
        return self.create_response(request, bundle)


class InvitationResource(ModelResource):
    sender_id = fields.IntegerField(attribute='sender__id', null=True)

    class Meta:
        queryset = Invitation.objects.all()
        resource_name = 'invitations'
        detail_uri_name = 'token_uuid'
        # authentication = SessionAuthentication()
        authorization = Authorization()
        filtering = {
            'group_id': ALL
        }

    def get_group(self, group_id):
        group_list = Group.objects.filter(id=int(group_id))
        return None if len(group_list) == 0 else group_list[0]

    def is_manager_group(self, group):
        return not group.extendedgroup.is_managed()

    def user_authorized(self, user, group):
        if self.is_manager_group(group):
            return user in group.user_set.all()
        else:
            return user in group.extendedgroup.manager_group.user_set.all()

    def has_expired(self, token):
        if token.expires is None:
            return True

        return (datetime.datetime.now() - token.expires).total_seconds() >= 0

    def update_db(self):
        for i in Invitation.objects.all():
            if self.has_expired(i):
                i.delete()

    def send_email(self, request, invitation):
        group = self.get_group(invitation.group_id)
        subject = "Invitation to join group {}".format(group.name)
        temp_loader = loader.get_template(
            'group_invitation/group_invite_email.txt')
        context_dict = {
            'group_name': group.name,
            'site': get_current_site(request),
            'token': invitation.token_uuid
        }
        email = EmailMessage(
            subject,
            temp_loader.render(Context(context_dict)),
            to=[invitation.recipient_email]
        )
        email.send()
        return HttpCreated("Email sent")

    # Filter to only show own resources.
    def obj_get_list(self, bundle, **kwargs):
        self.update_db()
        get_dict = bundle.request.GET
        user = bundle.request.user

        auth_group_list = filter(
            lambda g: self.user_authorized(user, g),
            user.groups.all())

        if get_dict.get('group_id'):
            auth_group_list = filter(
                lambda g:
                get_dict['group_id'].isdigit() and
                g.id == int(get_dict['group_id']),
                auth_group_list)

        auth_group_id_set = map(lambda g: g.id, auth_group_list)

        inv_list = filter(
            lambda i: i.group_id in auth_group_id_set,
            Invitation.objects.all())

        inv_list.sort(key=lambda i: i.id)
        return inv_list

    # Handle POST requests for sending tokens.
    def obj_create(self, bundle, **kwargs):
        self.update_db()
        request = bundle.request
        data = json.loads(request.raw_post_data)
        user = request.user
        group = self.get_group(int(data['group_id']))

        if not self.user_authorized(user, group):
            raise ImmediateHttpResponse(HttpUnauthorized())

        inv = Invitation(token_uuid=uuid.uuid1(), group_id=group.id)
        now = datetime.datetime.now()
        token_duration = datetime.timedelta(days=settings.TOKEN_DURATION)
        inv.expires = now + token_duration
        inv.sender = user
        inv.recipient_email = data['email']
        inv.save()
        self.send_email(request, inv)
        return bundle

    # Handle PUT requests for resending tokens.
    # Token UUIDs are embedded in URL for PUT requests.
    def obj_update(self, bundle, **kwargs):
        self.update_db()
        inv_list = Invitation.objects.filter(token_uuid=kwargs['token_uuid'])

        if len(inv_list) == 0:
            raise ImmediateHttpResponse(HttpNotFound('Not found or expired'))

        inv = inv_list[0]
        now = datetime.datetime.now()
        token_duration = datetime.timedelta(days=settings.TOKEN_DURATION)
        inv.expires = now + token_duration
        inv.save()
        self.send_email(inv)
        return bundle


class ExtendedGroupResource(ModelResource):
    member_list = fields.ListField(attribute='member_list', null=True)
    perm_list = fields.ListField(attribute='perm_list', null=True)
    can_edit = fields.BooleanField(attribute='can_edit', default=False)
    manager_group_uuid = fields.CharField(attribute='manager_group_uuid',
                                          null=True)

    class Meta:
        queryset = ExtendedGroup.objects.all()
        resource_name = 'extended_groups'
        object_class = ExtendedGroup
        detail_uri_name = 'uuid'
        # authentication = SessionAuthentication()
        authorization = Authorization()

    # More low-level group access
    def _get_ext_group(self, uuid):
        eg_list = ExtendedGroup.objects.filter(uuid=uuid)
        return None if len(eg_list) == 0 else eg_list[0]

    # Wrap get_ext_group and raise error if cannot be found.
    def get_ext_group_or_fail(self, uuid):
        ext_group = self._get_ext_group(uuid)

        if ext_group:
            return ext_group
        else:
            raise ImmediateHttpResponse(HttpNotFound())

    def ext_groups_with_user(self, user, allow_manager_groups=False):
        if allow_manager_groups:
            return filter(
                lambda g: user in g.user_set.all(),
                ExtendedGroup.objects.all())
        else:
            return filter(
                lambda g:
                not g.is_manager_group() and user in g.user_set.all(),
                ExtendedGroup.objects.all())

    def user_authorized(self, user, ext_group):
        if ext_group.is_manager_group():
            return user in ext_group.user_set.all()
        else:
            return (ext_group.manager_group and
                    user in ext_group.manager_group.user_set.all())

    def get_member_list(self, ext_group):
        return map(
            lambda u: {
                'user_id': u.id,
                'uuid': u.userprofile.uuid,
                'username': u.username,
                'first_name': u.first_name,
                'last_name': u.last_name,
                'is_manager': self.user_authorized(u, ext_group)
            },
            ext_group.user_set.all())

    # Override ORM methods for customization.
    def obj_get(self, bundle, **kwargs):
        user = bundle.request.user
        ext_group = super(ExtendedGroupResource, self).obj_get(
            bundle, **kwargs)
        ext_group.can_edit = self.user_authorized(user, ext_group)
        ext_group.manager_group_uuid = (
            ext_group.uuid
            if ext_group.is_manager_group()
            else ext_group.manager_group.uuid
        )
        return ext_group

    def obj_get_list(self, bundle, **kwargs):
        user = bundle.request.user
        ext_group_list = super(ExtendedGroupResource, self).obj_get_list(
            bundle, **kwargs)

        for i in ext_group_list:
            i.can_edit = self.user_authorized(user, i)
            i.manager_group_uuid = (
                i.uuid if i.is_manager_group() else i.manager_group.uuid
            )
        return ext_group_list

    def get_object_list(self, bundle, **kwargs):
        ext_group_list = super(ExtendedGroupResource, self).get_object_list(
            bundle, **kwargs)
        # Currently set so that manager groups are filtered out.
        # This does not make sense semantically but somehow works.
        return ext_group_list.filter(managed_group=None)

    def obj_create(self, bundle, **kwargs):
        user = bundle.request.user
        data = json.loads(bundle.request.raw_post_data)
        new_ext_group = ExtendedGroup(name=data['name'])
        new_ext_group.save()
        new_ext_group.user_set.add(user)
        new_ext_group.manager_group.user_set.add(user)
        return bundle

    # Extra things
    def prepend_urls(self):
        return [
            url(r'^extended_groups/members/$',
                self.wrap_view('ext_groups_members_list'),
                name='api_ext_group_members_list'),
            url(r'^extended_groups/(?P<uuid>'
                r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
                r')/members/$',
                self.wrap_view('ext_groups_members_basic'),
                name='api_ext_group_members_basic'),
            url(r'^extended_groups/(?P<uuid>'
                r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
                r')/members/(?P<user_id>[0-9]+)/$',
                self.wrap_view('ext_groups_members_detail'),
                name='api_ext_group_members_detail'),
        ]

    def ext_groups_members_basic(self, request, **kwargs):
        ext_group = self.get_ext_group_or_fail(kwargs['uuid'])
        user = request.user

        if request.method == 'GET':
            ext_group.member_list = self.get_member_list(ext_group)
            ext_group.can_edit = self.user_authorized(user, ext_group)
            ext_group.manager_group_uuid = (
                ext_group.uuid
                if ext_group.is_manager_group()
                else ext_group.manager_group.uuid
            )
            bundle = self.build_bundle(obj=ext_group, request=request)
            return self.create_response(request, self.full_dehydrate(bundle))
        elif request.method == 'PATCH':
            if not self.user_authorized(user, ext_group):
                return HttpUnauthorized()

            data = json.loads(request.raw_post_data)
            new_member_list = data['member_list']

            # Remove old members before updating.
            ext_group.user_set.clear()

            for m in new_member_list:
                ext_group.user_set.add(int(m['user_id']))

            # Managers should be in the groups they manage.
            if ext_group.is_manager_group():
                for g in ext_group.managed_group.all():
                    for m in new_member_list:
                        g.user_set.add(int(m['user_id']))

            return HttpAccepted()
        elif request.method == 'POST':
            if not self.user_authorized(user, ext_group):
                return HttpUnauthorized()

            data = json.loads(request.raw_post_data)
            new_member = data['user_id']
            ext_group.user_set.add(new_member)

            if ext_group.is_manager_group():
                for g in ext_group.managed_group.all():
                    g.user_set.add(new_member)

            return HttpAccepted()
        else:
            return HttpMethodNotAllowed()

    def ext_groups_members_detail(self, request, **kwargs):
        ext_group = self.get_ext_group_or_fail(kwargs['uuid'])
        user = request.user

        if request.method == 'GET':
            return self.ext_groups_members_basic(self, request, **kwargs)
        elif request.method == 'DELETE':
            # Removing yourself - must delete to leave if last member.
            if user.id == int(kwargs['user_id']):
                if ext_group.user_set.count() == 1:
                    return HttpForbidden('Last member - must delete group')

                # When demoting yourself while targetting manager group.
                if (ext_group.is_manager_group() and
                        ext_group.user_set.count() == 1):
                    return HttpForbidden(
                        'Last manager must delete group to leave')

                if (not ext_group.is_manager_group() and
                        user in ext_group.manager_group.user_set.all() and
                        ext_group.manager_group.user_set.count() == 1):
                    return HttpForbidden(
                        'Last manager must delete group to leave')

                ext_group.user_set.remove(user)

                if not ext_group.is_manager_group():
                    ext_group.manager_group.user_set.remove(user)

                return HttpNoContent()

            # Removing other people from the group
            else:
                if not self.user_authorized(user, ext_group):
                    return HttpUnauthorized()

                if ext_group.is_manager_group():
                    ext_group.user_set.remove(int(kwargs['user_id']))
                else:
                    ext_group.user_set.remove(int(kwargs['user_id']))
                    ext_group.manager_group.user_set.remove(
                        int(kwargs['user_id']))
                return HttpNoContent()
        else:
            return HttpMethodNotAllowed()

    def ext_groups_members_list(self, request, **kwargs):
        user = request.user

        if request.method == 'GET':
            ext_group_list = self.ext_groups_with_user(user)

            for i in ext_group_list:
                i.member_list = self.get_member_list(i)
                i.can_edit = self.user_authorized(user, i)
                i.manager_group_uuid = (
                    i.uuid if i.is_manager_group() else i.manager_group.uuid
                )
            return self.create_response(
                request,
                {
                    'meta': {
                        'total_count': len(ext_group_list)
                    },
                    'objects': map(
                        lambda g: self.full_dehydrate(
                            self.build_bundle(obj=g, request=request)),
                        ext_group_list)
                }
            )
        else:
            return HttpMethodNotAllowed()


# TODO: modify to be backed by a DB eventually.
class FastQCResource(Resource):
    data = fields.DictField(attribute='data', null=True)

    class Meta:
        resource_name = 'fastqc'
        object_class = FastQC
        detail_uri_name = 'analysis_uuid'
        authentication = SessionAuthentication()

    def is_float(self, value):
        try:
            float(value)
            return True
        except:
            return False

    def obj_get(self, bundle, **kwargs):
        user = bundle.request.user

        analysis = Analysis.objects.get(uuid=kwargs['analysis_uuid'])

        if not analysis:
            return HttpNotFound('Analysis not found')

        analysis_results = analysis.results.all()

        # For now assume fastqc files have a .txt extension.
        fastqc_file_list = filter(
            lambda f: '.txt' in f.file_name, analysis_results)

        if len(fastqc_file_list) == 0:
            return HttpNotFound("Unable to find matching FastQC file")

        # Pick the first one because there's only supposed to be one.
        fastqc_file = FileStoreItem.objects.get(
            uuid=fastqc_file_list[0].file_store_uuid)

        if not fastqc_file:
            return HttpNotFound(
                "Unable to find matching FastQC file in File Store")

        fastqc_file_obj = fastqc_file.get_file_object()
        fadapa_input = fastqc_file_obj.read().splitlines()

        parser = Fadapa(fadapa_input)

        tmp_dict = {'Summary': {}}

        for i in parser.summary()[1:]:
            tmp_dict['Summary'][i[0].lower().replace(' ', '_')] = {
                'title': i[0],
                'result': i[1]
            }
            parsed_data = []

            try:
                parsed_data = parser.clean_data(i[0])
            except:
                logger.warn(
                    "No data found for " + i[0] + " in file " +
                    fastqc_file_list[0].file_store_uuid
                )

            clean_data = []

            for row in parsed_data:
                clean_data.append(row[0:1] + map(
                    lambda d:
                    float(d) if self.is_float(d) else d,
                    row[1:]))

            tmp_dict[i[0]] = clean_data

        # Modify the 'Basic Statistics' module if it exists.
        if tmp_dict.get('Basic Statistics'):
            mod = {}

            for i in tmp_dict['Basic Statistics']:
                mod[i[0]] = i[1]

            tmp_dict['Basic Statistics'] = mod

        # Rename to friendly format dict keys.
        new = {}

        for key in tmp_dict:
            new[key.lower().replace(' ', '_')] = tmp_dict[key]

        return FastQC(new)
