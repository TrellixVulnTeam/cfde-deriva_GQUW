#!/usr/bin/python3

"""Translate basic Frictionless Table-Schema table definitions to Deriva."""

import os
import sys
import json
import hashlib
import base64
from deriva.core import tag, AttrDict
from deriva.core.ermrest_model import builtin_types, Table, Column, Key, ForeignKey

if 'source_definitions' not in tag:
    # monkey-patch this newer annotation key until it appears in deriva-py
    tag['source_definitions'] = 'tag:isrd.isi.edu,2019:source-definitions'
if 'history_capture' not in tag:
    tag['history_capture'] = 'tag:isrd.isi.edu,2020:history-capture'

# some useful authentication IDs to use in preparing ACLs...
authn_id = AttrDict({
    # CFDE roles
    "cfde_portal_admin": "https://auth.globus.org/5f742b05-9210-11e9-aa27-0e4b2da78b7a",
    "cfde_portal_curator": "https://auth.globus.org/b5ff40d0-9210-11e9-aa1a-0a294aef5614",
    "cfde_portal_writer": "https://auth.globus.org/e8d6b111-9210-11e9-aa1a-0a294aef5614",
    "cfde_portal_creator": "https://auth.globus.org/f4c5c479-a8bf-11e9-a6e2-0a075bc69d14",
    "cfde_portal_reader": "https://auth.globus.org/1f8a9ec5-9211-11e9-bc6f-0aaa2b1d1516",
    "cfde_portal_reviewer": "TBD: read-only reviewers of unreleased submissions",
    "cfde_infrastructure_ops": "https://auth.globus.org/7116589f-3a72-11eb-86d2-0aa357bce76b",
    "cfde_submission_pipeline": "https://auth.globus.org/1fd07875-3f06-11eb-8761-0ece49b2bd8d",
    "cfde_action_provider": "https://auth.globus.org/21017803-059f-4a9b-b64c-051ab7c1d05d",
})

cfde_portal_viewers = {
    authn_id.cfde_portal_admin,
    authn_id.cfde_portal_curator,
    authn_id.cfde_portal_writer,
    authn_id.cfde_portal_reviewer,
}

def _attrdict_from_strings(*strings):
    new = AttrDict()
    for prefix, term in [ s.split(':') for s in strings ]:
        if prefix not in new:
            new[prefix.replace('-', '_')] = AttrDict()
        if term.replace('-', '_') not in new[prefix.replace('-', '_')]:
            new[prefix.replace('-', '_')][term.replace('-', '_')] = '%s:%s' % (prefix, term)
    return new

# structured access to controlled terms we will use in this code...
terms = _attrdict_from_strings(
    'cfde_registry_grp_role:admin',
    'cfde_registry_grp_role:submitter',
    'cfde_registry_grp_role:review-decider',
    'cfde_registry_grp_role:reviewer',
    'cfde_registry_dp_status:submitted',
    'cfde_registry_dp_status:ops-error',
    'cfde_registry_dp_status:bag-valid',
    'cfde_registry_dp_status:bag-error',
    'cfde_registry_dp_status:check-valid',
    'cfde_registry_dp_status:check-error',
    'cfde_registry_dp_status:content-ready',
    'cfde_registry_dp_status:content-error',
    'cfde_registry_dp_status:rejected',
    'cfde_registry_dp_status:release-pending',
    'cfde_registry_dp_status:obsoleted',
    'cfde_registry_dpt_status:enumerated',
    'cfde_registry_dpt_status:name-error',
    'cfde_registry_dpt_status:data-absent',
    'cfde_registry_dpt_status:check-error',
    'cfde_registry_dpt_status:content-ready',
    'cfde_registry_dpt_status:content-error',
)

def acls_union(*sources):
    """Produce union of aclsets"""
    acls = {}
    for aclset in sources:
        for aclname, acl in aclset.items():
            existing = set(acls.setdefault(aclname, []))
            # remap built-in authn IDs as a convenience
            additional = { authn_id.get(attr, attr) for attr in acl }
            acls[aclname].extend(additional.difference(existing))
    return acls

def aclbindings_merge(*sources):
    """Produce merge of source acl bindings"""
    bindings = {}
    for bindings in sources:
        for bname, binding in bindings.items():
            if isinstance(binding, dict):
                binding = dict(binding)
                if 'scope_acl' in binding:
                    binding['scope_acl'] = [ authn_id.get(attr, attr) for attr in binding['scope_acl'] ]
            bindings[bname] = binding
    return bindings

def multiplexed_acls_union(*sources):
    """Produce union of multiplexed aclsets"""
    keys = set.union(*[ set(src.keys()) for src in sources ])
    return {
        key: acls_union(*[ src.get(key, {}) for src in sources ])
        for key in keys
    }

def multiplexed_aclbindings_merge(*sources):
    """Produce merge of multiplexed acl bindings"""
    keys = set.union(*[ set(src.keys()) for src in sources ])
    return {
        key: aclbindings_merge(*[ src.get(key, {}) for src in sources ])
        for key in keys
    }

class CatalogConfigurator (object):

    # our baseline policy for everything we operate in CFDE
    catalog_acls = {
        "owner": [ authn_id.cfde_infrastructure_ops ],
        "create": [],
        "select": [],
        "write": [],
        "insert": [],
        "update": [],
        "delete": [],
        "enumerate": [ "*" ],
    }
    schema_acls = {
        "CFDE": { "select": [ authn_id.cfde_portal_admin ] },
        "public": { "select": [] },
    }
    schema_table_acls = {}
    schema_table_aclbindings = {}
    schema_table_column_acls = {}
    schema_table_column_aclbindings = {}

    def __init__(self, catalog=None, registry=None):
        """Construct a configurator
        """
        self.catalog = None
        self.registry = None
        self.set_catalog(catalog, registry)

    def set_catalog(self, catalog, registry=None):
        if self.catalog is catalog and self.registry is registry:
            return
        self.catalog = catalog
        self.registry = registry
        # copy our class-level ACLs which we might mutate!
        self.catalog_acls = acls_union(self.catalog_acls)
        # be careful with owner ACL
        # ermrest will not allow us to drop our ownership!
        session = catalog.get_authn_session().json()
        my_attr_ids = { a['id'] for a in session['attributes'] }
        planned_owner = set(self.catalog_acls['owner'])
        existing_owner = set(catalog.get('/acl').json()['owner'])
        if not planned_owner.intersection(my_attr_ids):
            # our designed config won't be allowed, so augment it
            # copy our class-level config before mutating!
            self.catalog_acls = dict(self.catalog_acls)
            self.catalog_acls['owner'] = list(self.catalog_acls['owner'])
            self.catalog_acls['owner'].extend(
                # use whatever portion of existing owner ACL would match current client
                existing_owner.intersection(my_attr_ids)
            )

    def apply_acls_to_obj(self, obj, acls, replace):
        newacls = acls if replace else acls_union(obj.acls, acls)
        obj.acls.clear()
        obj.acls.update(newacls)

    def apply_aclbindings_to_obj(self, obj, bindings, replace):
        newbinds = bindings if replace else aclbiindings_merge(obj.acl_bindings, bindings)
        obj.acl_bindings.clear()
        obj.acl_bindings.update(newbinds)

    def get_review_acl(self):
        acl = set(cfde_portal_viewers)
        for record in self.registry.get_groups_by_dcc_role():
            # record is like dict(dcc=dcc_id, role=role_id, groups=[...])
            acl.update({ grp['webauthn_id'] for grp in record['groups'] })
        return sorted(list(acl))

    def apply_chaise_config(self, model):
        model.annotations[tag.chaise_config] = {
            #"navbarBrandText": "CFDE Data Browser",
            "SystemColumnsDisplayCompact": [],
            "SystemColumnsDisplayDetailed": [],
            "navbarMenu": {
                "children": [
                    {
                        "name": "Browse All Data",
                        "children": [
                            { "name": "Collection", "url": "/chaise/recordset/#%s/CFDE:collection" % self.catalog._catalog_id },
                            { "name": "File", "url": "/chaise/recordset/#%s/CFDE:file" % self.catalog._catalog_id },
                            { "name": "Biosample", "url": "/chaise/recordset/#%s/CFDE:biosample" % self.catalog._catalog_id },
                            { "name": "Subject", "url": "/chaise/recordset/#%s/CFDE:subject" % self.catalog._catalog_id },
                            { "name": "Project", "url": "/chaise/recordset/#%s/CFDE:project" % self.catalog._catalog_id },
                            {
                                "name": "Vocabulary",
                                "children": [
                                    { "name": "Anatomy", "url": "/chaise/recordset/#%s/CFDE:anatomy" % self.catalog._catalog_id },
                                    { "name": "Assay Type", "url": "/chaise/recordset/#%s/CFDE:assay_type" % self.catalog._catalog_id },
                                    { "name": "Data Type", "url": "/chaise/recordset/#%s/CFDE:data_type" % self.catalog._catalog_id },
                                    { "name": "File Format", "url": "/chaise/recordset/#%s/CFDE:file_format" % self.catalog._catalog_id },
                                    { "name": "NCBI Taxonomy", "url": "/chaise/recordset/#%s/CFDE:ncbi_taxonomy" % self.catalog._catalog_id },
                                    { "name": "Subject Granularity", "url": "/chaise/recordset/#%s/CFDE:subject_granularity" % self.catalog._catalog_id },
                                    { "name": "Subject Role", "url": "/chaise/recordset/#%s/CFDE:subject_role" % self.catalog._catalog_id },
                                ]
                            },
                            { "name": "ID Namespace", "url": "/chaise/recordset/#%s/CFDE:id_namespace" % self.catalog._catalog_id },
                        ]
                    },
                    { "name": "Technical Documentation", "markdownName": ":span:Technical Documentation:/span:{.external-link-icon}", "url": "https://cfde-published-documentation.readthedocs-hosted.com/en/latest/" },
                    { "name": "User Guide", "markdownName": ":span:User Guide:/span:{.external-link-icon}", "url": "https://cfde-published-documentation.readthedocs-hosted.com/en/latest/about/portalguide/" },
                    { "name": "About CFDE", "markdownName": ":span:About CFDE:/span:{.external-link-icon}", "url": "https://cfde-published-documentation.readthedocs-hosted.com/en/latest/about/CODEOFCONDUCT/"},
                    { "name": "|" },
                    { "name": "Dashboard", "url": "/dashboard.html" },
                    {
                        "name": "Data Review",
                        "url": "/chaise/recordset/#registry/CFDE:datapackage",
                        "acls": {
                            "enable": self.get_review_acl(),
                        }
                    }
                ]
            }
        }

    def apply_to_model(self, model, replace=True):
        # set custom chaise configuration values
        if replace or tag.chaise_config not in model.annotations:
            self.apply_chaise_config(model)

        def _update(parent, key, d):
            if key not in parent:
                parent[key] = dict()
            parent[key].update(d)

        # have Chaise display underscores in model element names as whitespace
        _update(
            model.schemas['CFDE'].display,
            "name_style",
            {
                "underline_space": True,
                "title_case": True,
            }
        )
        # turn off clutter of many links in tabular views
        _update(
            model.schemas['CFDE'].display,
            "show_foreign_key_link",
            {
                "compact": False
            }
        )

        # prettier display of built-in ERMrest_Client table entries
        if 'public' in model.schemas \
           and 'ERMrest_Client' in model.schemas['public'].tables:
            _update(
                model.table('public', 'ERMrest_Client').table_display,
                'row_name',
                {"row_markdown_pattern": "{{{Full_Name}}} ({{{Display_Name}}})"}
            )

        self.apply_acls_to_obj(model, self.catalog_acls, replace=True)
        for sname, acls in self.schema_acls.items():
            try:
                self.apply_acls_to_obj(model.schemas[sname], acls, replace=True)
            except KeyError:
                pass

        for stname, acls in self.schema_table_acls.items():
            sname, tname = stname
            try:
                self.apply_acls_to_obj(model.schemas[sname].tables[tname], acls, replace=replace)
            except KeyError:
                pass

        for stname, binds in self.schema_table_aclbindings.items():
            sname, tname = stname
            try:
                self.apply_aclbindings_to_obj(model.schemas[sname].tables[tname], binds, replace=replace)
            except KeyError:
                pass

        for stcname, acls in self.schema_table_column_acls.items():
            sname, tname, cname = stcname
            try:
                self.apply_acls_to_obj(model.schemas[sname].tables[tname].columns[cname], acls, replace=replace)
            except KeyError:
                pass

        for stcname, binds in self.schema_table_column_aclbindings.items():
            sname, tname, cname = stcname
            try:
                self.apply_aclbindings_to_obj(model.schemas[sname].tables[tname].columns[cname], binds, replace=replace)
            except KeyError:
                pass

class ReleaseConfigurator (CatalogConfigurator):

    # release catalogs allow public read-access on entire CFDE schema
    schema_acls = multiplexed_acls_union(
        CatalogConfigurator.schema_acls,
        {
            "CFDE": { "select": ["*"] },
        }
    )

    def __init__(self, catalog=None, registry=None):
        super(ReleaseConfigurator, self).__init__(catalog, registry)

class ReviewConfigurator (CatalogConfigurator):

    # set consistent ownership for automation
    catalog_acls = acls_union(
        CatalogConfigurator.catalog_acls,
        {
            "owner": [ authn_id.cfde_infrastructure_ops, authn_id.cfde_submission_pipeline ],
        }
    )

    # SEE schema_acls property below!

    def __init__(self, catalog=None, registry=None, submission_id=None):
        super(ReviewConfigurator, self).__init__(catalog, registry)
        self.submission_id = submission_id

    @property
    def schema_acls(self):
        # review catalogs allow CFDE-CC roles to read entire CFDE schema
        acls = multiplexed_acls_union(
            CatalogConfigurator.schema_acls,
            {
                "CFDE": { "select": list(cfde_portal_viewers) },
            }
        )
        if self.registry is not None and self.submission_id is not None:
            metadata = self.registry.get_datapackage(self.submission_id)
            dcc_read_acl = list(set.union(*[
                set(self.registry.get_dcc_acl(metadata['submitting_dcc'], role))
                for role in {
                        terms.cfde_registry_grp_role.admin,
                        terms.cfde_registry_grp_role.reviewer,
                        terms.cfde_registry_grp_role.review_decider,
                }
            ]))
            # review catalogs allow DCC-specific read-access on entire CFDE schema
            acls = multiplexed_acls_union(
                acls,
                {
                    "CFDE": { "select": dcc_read_acl },
                }
            )
        return acls

    def get_review_acl(self):
        # restrict navbar ACL to match our content
        return self.schema_acls["CFDE"]["select"]

    def apply_chaise_config(self, model):
        """Apply custom chaise config for review content by adjusting the standard config"""
        super(ReviewConfigurator, self).apply_chaise_config(model)

        # trim off standard navbar content we want to replace
        del model.annotations[tag.chaise_config]['navbarMenu']['children'][-1] # Data Review link...
        del model.annotations[tag.chaise_config]['navbarMenu']['children'][-1] # Dashboard link...

        # add custom navbar info
        datapackage = self.registry.get_datapackage(self.submission_id)
        dcc = self.registry.get_dcc(datapackage['submitting_dcc'])[0]

        def registry_record_page(tname, rid=None):
            url = self.registry._catalog.get_server_uri()
            url= url.replace('/ermrest/catalog/', '/chaise/record/#')
            if url[-1] != '/':
                url += '/'
            url += 'CFDE:%s' % (tname,)
            if rid is not None:
                url += '/RID=%s' % (rid,)
            return url

        model.annotations[tag.chaise_config]['navbarMenu']['children'][0].update({
            "name": "Browse Submitted Data",
            "acls": {
                "enable": self.get_review_acl(),
            },
        })
        model.annotations[tag.chaise_config]['navbarMenu']['children'].append({
            "name": "In-Review Submission",
            "acls": {
                "enable": self.get_review_acl(),
            },
            "children": [
                {
                    "name": "Content Summary Charts",
                    # we need to fake this since we configure before the review_summary_url is populated
                    "url": "/dcc_review.html?catalogId=%s" % self.catalog.catalog_id
                },
                {
                    "name": "Submission %s" % datapackage['id'],
                    "url": registry_record_page('datapackage', datapackage['RID']),
                },
                {
                    "name": dcc["dcc_name"],
                    "url": registry_record_page('dcc', dcc['RID']),
                },
            ]
        })

class RegistryConfigurator (CatalogConfigurator):

    schema_acls = multiplexed_acls_union(
        CatalogConfigurator.schema_acls,
        {
            # portal admin can adjust most registry content by hand
            # portal curator can see most registry content
            'CFDE': {
                'select': [ "*" ],
                'insert': [ authn_id.cfde_portal_admin ],
                'update': [ authn_id.cfde_portal_admin ],
                'delete': [ authn_id.cfde_portal_admin ]
            }
        }
    )

    schema_table_acls = multiplexed_acls_union(
        CatalogConfigurator.schema_table_acls,
        {
            # make client table visible for provenance presentation...
            ('public', 'ERMrest_Client'): {
                "select": [ "*" ],
                "insert": [ authn_id.cfde_portal_admin, authn_id.cfde_submission_pipeline ],
            },
        }
    )

    schema_table_column_acls = multiplexed_acls_union(
        CatalogConfigurator.schema_table_acls,
        {
            # ... but hide sensitive client table cols
            ('public', 'ERMrest_Client', 'Email'): {
                "select": [ authn_id.cfde_portal_admin, authn_id.cfde_portal_curator ],
                "enumerate": [ authn_id.cfde_portal_admin, authn_id.cfde_portal_curator ],
            },
            ('public', 'ERMrest_Client', 'Client_Object'): {
                "select": [],
                "enumerate": [ authn_id.cfde_submission_pipeline ],
            },
        }
    )

    schema_table_aclbindings = multiplexed_aclbindings_merge(
        CatalogConfigurator.schema_table_aclbindings,
        {
            ('public', 'ERMrest_Client'): {
                "self_view": {
                    "types": ["select"],
                    "projection": ["ID"],
                    "projection_type": "acl",
                    "scope_acl": ["*"],
                }
            }
        }
    )

    def __init__(self, catalog=None, registry=None):
        super(RegistryConfigurator, self).__init__(catalog, registry)

    def apply_chaise_config(self, model):
        """Apply custom chaise config for registry by adjusting the standard config"""
        super(RegistryConfigurator, self).apply_chaise_config(model)

        # custom config for submission listings
        model.annotations[tag.chaise_config]['maxRecordsetRowHeight'] = 350

        # trim off standard navbar content we want to replace
        del model.annotations[tag.chaise_config]['navbarMenu']['children'][-1] # Data Review link...
        del model.annotations[tag.chaise_config]['navbarMenu']['children'][-1] # Dashboard link...

        # fixup incorrectly generated "Browse All Data" links
        def fixup(*entries):
            for entry in entries:
                if 'url' in entry:
                    entry['url'] = entry['url'].replace('#registry/', '#1/')
                elif 'children' in entry:
                    fixup(*entry['children'])

        fixup(model.annotations[tag.chaise_config]['navbarMenu']['children'][0])

        model.annotations[tag.chaise_config]['navbarMenu']['children'].append({
            "name": "Submission System",
            "acls": {
                "enable": self.get_review_acl(),
            },
            "children": [
                { "name": "Releases", "url": "/chaise/recordset/#registry/CFDE:release" },
                { "name": "Submitted datapackages", "url": "/chaise/recordset/#registry/CFDE:datapackage" },
                { "name": "Enrolled DCCs", "url": "/chaise/recordset/#registry/CFDE:dcc" },
                { "name": "Enrolled groups", "url": "/chaise/recordset/#registry/CFDE:group" },
                #{ "name": "Enrolled namespaces", "url": "/chaise/recordset/#registry/CFDE:id_namespace" },
                {
                    "name": "Vocabulary",
                    "children": [
                        { "name": "Release status", "url": "/chaise/recordset/#registry/CFDE:release_status" },
                        { "name": "Datapackage status", "url": "/chaise/recordset/#registry/CFDE:datapackage_status" },
                        { "name": "Table status", "url": "/chaise/recordset/#registry/CFDE:datapackage_table_status" },
                        { "name": "Approval status", "url": "/chaise/recordset/#registry/CFDE:approval_status" },
                        { "name": "Group role", "url": "/chaise/recordset/#registry/CFDE:group_role" },
                        #{ "name": "Namespace role", "url": "/chaise/recordset/#registry/CFDE:id_namespace_role" },
                    ]
                },
            ]
        })

schema_tag = 'tag:isrd.isi.edu,2019:table-schema-leftovers'
resource_tag = 'tag:isrd.isi.edu,2019:table-resource'

# translate table-schema definitions into deriva definitions
schema_name = 'CFDE'

def make_type(type, format):
    """Choose appropriate ERMrest column types..."""
    if type == "string":
        return builtin_types.text
    if type == "datetime":
        return builtin_types.timestamptz
    if type == "date":
        return builtin_types.date
    if type == "integer":
        return builtin_types.int8
    if type == "number":
        return builtin_types.float8
    if type == "list":
        # assume a list is a list of strings for now...
        return builtin_types["text[]"]
    raise ValueError('no mapping defined yet for type=%s format=%s' % (type, format))

def make_column(tname, cdef, configurator):
    cdef = dict(cdef)
    constraints = cdef.get("constraints", {})
    cdef_name = cdef.pop("name")
    title = cdef.get("title", None)
    nullok = not constraints.pop("required", False)
    default = cdef.pop("default", None)
    description = cdef.pop("description", None)
    annotations = {
        schema_tag: cdef,
    }
    if title is not None:
        annotations[tag.display] = {"name": title}
    pre_annotations = cdef.get("deriva", {})
    for k, t in tag.items():
        if k in pre_annotations:
            annotations[t] = pre_annotations.pop(k)
    acls = acls_union(
        configurator.schema_table_column_acls.get( (schema_name, tname, cdef_name), {} ),
        pre_annotations.pop('acls', {})
    )
    acl_bindings = aclbindings_merge(
        configurator.schema_table_column_aclbindings.get( (schema_name, tname, cdef_name), {} ),
        pre_annotations.pop('acl_bindings', {})
    )
    return Column.define(
        cdef_name,
        make_type(
            cdef.get("type", "string"),
            cdef.get("format", "default"),
        ),
        nullok=nullok,
        default=default,
        comment=description,
        annotations=annotations,
        acls=acls,
        acl_bindings=acl_bindings,
    )

def make_id(*components):
    """Build an identifier that will be OK for ERMrest and Postgres.

    Naively, append as '_'.join(components).

    Fallback to ugly hashing to try to shorten long identifiers.
    """
    expanded = []
    for e in components:
        if isinstance(e, list):
            expanded.extend(e)
        else:
            expanded.append(e)
    result = '_'.join(expanded)
    if len(result.encode('utf8')) <= 63:
        # happy path, use naive name as requested
        return result
    else:
        # we have to shorten this id
        truncate_threshold = 4
        def helper(e):
            if len(e) <= truncate_threshold:
                # retain short elements
                return e
            else:
                # replace long elements with truncated MD5 hash
                h = hashlib.md5()
                h.update(e.encode('utf8'))
                return base64.b64encode(h.digest()).decode()[0:truncate_threshold]
        truncated = list(expanded)
        for i in range(len(truncated)):
            truncated[-1 - i] = helper(truncated[-1 - i])
            result = '_'.join(truncated)
            if len(result.encode('utf8')) <= 63:
                return result
    raise NotImplementedError('Could not generate valid ID for components "%r"' % expanded)

def make_key(tname, cols):
    return Key.define(
        cols,
        constraint_names=[[ schema_name, make_id(tname, cols, 'key') ]],
    )

def make_fkey(tname, fkdef):
    fkcols = fkdef.pop("fields")
    fkcols = [fkcols] if isinstance(fkcols, str) else fkcols
    reference = fkdef.pop("reference")
    pkschema = reference.pop("resourceSchema", schema_name)
    pktable = reference.pop("resource")
    pktable = tname if pktable == "" else pktable
    to_name = reference.pop("title", None)
    pkcols = reference.pop("fields")
    pkcols = [pkcols] if isinstance(pkcols, str) else pkcols
    constraint_name = fkdef.pop("constraint_name", None)
    if constraint_name is None:
        # don't run this if we don't need it...
        constraint_name = make_id(tname, fkcols, 'fkey')
    if len(constraint_name.encode('utf8')) > 63:
        raise ValueError('Constraint name "%s" too long in %r' % (constraint_name, fkdef))
    def get_action(clause):
        try:
            return {
                'cascade': 'CASCADE',
                'set null': 'SET NULL',
                'set default': 'SET DEFAULT',
                'restrict': 'RESTRICT',
                'no action': 'NO ACTION',
            }[fkdef.pop(clause, 'no action').lower()]
        except KeyError as e:
            raise ValueError('unknown action "%s" for foreign key %s %s clause' % (e, constraint_name, clause))
    on_delete = get_action('on_delete')
    on_update = get_action('on_update')
    annotations = {
        schema_tag: fkdef,
    }
    if to_name is not None:
        annotations[tag.foreign_key] = {"to_name": to_name}
    return ForeignKey.define(
        fkcols,
        pkschema,
        pktable,
        pkcols,
        constraint_names=[[ schema_name, constraint_name ]],
        on_delete=on_delete,
        on_update=on_update,
        annotations=annotations
    )

def make_table(tdef, configurator, trusted=False, history_capture=False):
    provide_system = not (os.getenv('SKIP_SYSTEM_COLUMNS', 'false').lower() == 'true')
    tname = tdef["name"]
    if provide_system:
        system_columns = Table.system_column_defs()
        # bypass bug in deriva-py producing invalid default constraint name for system key
        #system_keys = Table.system_key_defs()
        system_keys = [ make_key(tname, ['RID']) ]
        # customize the system column templates...
        for col in system_columns:
            cname = col['name']
            col['comment'] = {
                'RID': 'Immutable record identifier (system-generated).',
                'RCT': 'Record creation time (system-generated).',
                'RMT': 'Record last-modification time (system-generated).',
                'RCB': 'Record created by (system-generated).',
                'RMB': 'Record last-modified by (system-generated).',
            }[cname]
            display_names = {
                'RCT': 'Creation Time',
                'RMT': 'Modification Time',
                'RCB': 'Created By',
                'RMB': 'Modified By',
            }
            if cname != 'RID':
                col['annotations'] = {tag.display: {"name": display_names[cname]}}
        system_fkeys = [
            ForeignKey.define(
                [cname], 'public', 'ERMrest_Client', ['ID'],
                constraint_names=[[ schema_name, make_id(tname, cname, 'fkey') ]]
            )
            for cname in ['RCB', 'RMB']
        ]
    else:
        system_columns = []
        system_keys = []
        system_fkeys = []
    tcomment = tdef.get("description")
    tdef_resource = tdef
    tdef = tdef_resource.pop("schema")
    keys = []
    keysets = set()
    pk = tdef.pop("primaryKey", None)
    if isinstance(pk, str):
        pk = [pk]
    if isinstance(pk, list):
        keys.append(make_key(tname, pk))
        keysets.add(frozenset(pk))
    tdef_fields = tdef.pop("fields", None)
    for cdef in tdef_fields:
        if cdef.get("constraints", {}).pop("unique", False):
            kcols = [cdef["name"]]
            if frozenset(kcols) not in keysets:
                keys.append(make_key(tname, kcols))
                keysets.add(frozenset(kcols))
    tdef_fkeys = tdef.pop("foreignKeys", [])
    title = tdef_resource.get("title", None)
    annotations = {
        resource_tag: tdef_resource,
        schema_tag: tdef,
    }
    if title is not None:
        annotations[tag.display] = {"name": title}
    pre_annotations = tdef_resource.get("deriva", {})
    for k, t in tag.items():
        if k == 'history_capture':
            annotations[t] = pre_annotations.pop('history_capture', history_capture) if trusted else history_capture
        elif k in pre_annotations and trusted:
            annotations[t] = pre_annotations.pop(k)
    acls = acls_union(
        configurator.schema_table_acls.get( (schema_name, tname), {} ),
        pre_annotations.pop('acls', {})
    )
    acl_bindings = aclbindings_merge(
        configurator.schema_table_aclbindings.get( (schema_name, tname), {} ),
        pre_annotations.pop('acl_bindings', {})
    )
    return Table.define(
        tname,
        column_defs=system_columns + [
            make_column(tname, cdef, configurator)
            for cdef in tdef_fields
        ],
        key_defs=system_keys + keys,
        fkey_defs=system_fkeys + [
            make_fkey(tname, fkdef)
            for fkdef in tdef_fkeys
        ],
        comment=tcomment,
        provide_system=False,
        annotations=annotations,
        acls=acls,
        acl_bindings=acl_bindings,
    )

def make_model(tableschema, configurator, trusted=False):
    resources = tableschema.pop('resources')
    rnames = {}
    for r in resources:
        if r["name"] in rnames:
            raise ValueError('Resource name "%s" appears more than once' % (r["name"],))
    pre_annotations = tableschema.get("deriva", {})
    history_capture = pre_annotations.pop('history_capture', False) if trusted else False
    annotations = {
        schema_tag: tableschema,
    }
    for k, t in tag.items():
        if k == 'history_capture':
            # we handled this above, don't blindly copy it
            continue
        if k in pre_annotations and trusted:
            annotations[t] = pre_annotations.pop(k)
    return {
        "schemas": {
            schema_name: {
                "schema_name": schema_name,
                "tables": {
                    tdef["name"]: make_table(tdef, configurator, trusted=trusted, history_capture=history_capture)
                    for tdef in resources
                },
                "acls": configurator.schema_acls.get(schema_name, {}),
            }
        },
        "annotations": annotations,
        "acls": configurator.catalog_acls,
    }

def main():
    """Translate basic Frictionless Table-Schema table definitions to Deriva.

    - Reads table-schema JSON on standard input
    - Writes deriva schema JSON on standard output

    The output JSON is suitable for POST to an /ermrest/catalog/N/schema
    resource on a fresh, empty catalog.

    Arguments:  [ { 'registry' | 'review' | 'release' } [ 'trusted' ] ]

    Examples:

    python3 -m cfde_deriva.tableschema release trusted < configs/portal/c2m2-level1-portal-model.json
    python3 -m cfde_deriva.tableschema review < configs/portal/c2m2-level1-portal-model.json
    python3 -m cfde_deriva.tableschema registry trusted < configs/registry/cfde-registry-model.json

    Optionally:

    run with SKIP_SYSTEM_COLUMNS=true to suppress generation of ERMrest
    system columns RID,RCT,RCB,RMT,RMB for each table.

"""
    if len(sys.argv) < 2:
        raise ValueError('missing required catalog-type argument: registry | review | release')

    configurator = {
        'release': ReleaseConfigurator,
        'review': ReviewConfigurator,
        'registry': RegistryConfigurator,
    }[sys.argv[1]]()

    if len(sys.argv) > 2:
        trusted = sys.argv[2].lower() == 'trusted'
    else:
        trusted = False

    json.dump(make_model(json.load(sys.stdin), configurator, trusted), sys.stdout, indent=2)
    return 0

if __name__ == '__main__':
    exit(main())
