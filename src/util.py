# Utility functions for migration scripts

from contextlib import contextmanager
import logging
from operator import itemgetter
from textwrap import dedent
import time

from docutils.core import publish_string
#import psycopg2

from openerp import release, SUPERUSER_ID
from openerp.addons.base.module.module import MyWriter
from openerp.modules.registry import RegistryManager
from openerp.tools.mail import html_sanitize

_logger = logging.getLogger(__name__)

@contextmanager
def savepoint(cr):
    name = hex(int(time.time() * 1000))[1:]
    cr.execute("SAVEPOINT %s" % (name,))
    try:
        yield
        cr.execute('RELEASE SAVEPOINT %s' % (name,))
    except Exception:
        cr.execute('ROLLBACK TO SAVEPOINT %s' % (name,))
        raise


def table_of_model(cr, model):
    return {
        'ir.actions.actions':          'ir_actions',
        'ir.actions.act_url':          'ir_act_url',
        'ir.actions.act_window':       'ir_act_window',
        'ir.actions.act_window_close': 'ir_actions',
        'ir.actions.act_window.view':  'ir_act_window_view',
        'ir.actions.client':           'ir_act_client',
        'ir.actions.report.xml':       'ir_act_report_xml',
        'ir.actions.server':           'ir_act_server',
        'ir.actions.wizard':           'ir_act_wizard',

        'stock.picking.in':  'stock_picking',
        'stock.picking.out': 'stock_picking',

        'workflow':            'wkf',
        'workflow.activity':   'wkf_activity',
        'workflow.instance':   'wkf_instance',
        'workflow.transition': 'wkf_transition',
        'workflow.triggers':   'wkf_triggers',
        'workflow.workitem':   'wkf_workitem',
    }.get(model, model.replace('.', '_'))


def remove_record(cr, name, deactivate=False, active_field='active'):
    if isinstance(name, str):
        if '.' not in name:
            raise ValueError('Please use fully qualified name <module>.<name>')
        module, _, name = name.partition('.')
        cr.execute("""DELETE FROM ir_model_data
                            WHERE module = %s
                              AND name = %s
                        RETURNING model, res_id
                   """, (module, name))
        data = cr.fetchone()
        if not data:
            return
        model, res_id = data
    elif isinstance(name, tuple):
        if len(name) != 2:
            raise ValueError('Please use a 2-tuple (<model>, <res_id>)')
        model, res_id = name
    else:
        raise ValueError('Either use a fully qualified xmlid string ' +
                         '<module>.<name> or a 2-tuple (<model>, <res_id>)')

    table = table_of_model(cr, model)
    try:
        with savepoint(cr):
            cr.execute('DELETE FROM "%s" WHERE id=%%s' % table, (res_id,))
    except Exception:
        if not deactivate or not active_field:
            raise
        cr.execute('UPDATE "%s" SET "%s"=%%s WHERE id=%%s' % (table, active_field), (False, res_id))
    else:
        # TODO delete attachments & workflow instances
        pass

def ref(cr, xmlid):
    if '.' not in xmlid:
        raise ValueError('Please use fully qualified name <module>.<name>')

    module, _, name = xmlid.partition('.')
    cr.execute("""SELECT res_id
                    FROM ir_model_data
                   WHERE module = %s
                     AND name = %s
                """, (module, name))
    data = cr.fetchone()
    if data:
        return data[0]
    return None

def ensure_xmlid_match_record(cr, xmlid, model, values):
    if '.' not in xmlid:
        raise ValueError('Please use fully qualified name <module>.<name>')

    module, _, name = xmlid.partition('.')
    cr.execute("""SELECT id, res_id
                    FROM ir_model_data
                   WHERE module = %s
                     AND name = %s
                """, (module, name))

    table = table_of_model(cr, model)
    data = cr.fetchone()
    if data:
        data_id, res_id = data
        # check that record still exists
        cr.execute("SELECT id FROM %s WHERE id=%%s" % table, (res_id,))
        if cr.fetchone():
            return res_id
    else:
        data_id = None

    # search for existing record marching values
    where = []
    data = ()
    for k, v in values.items():
        if v:
            where += ['%s = %%s' % (k,)]
            data += (v,)
        else:
            where += ['%s IS NULL' % (k,)]
            data += ()

    query = ("SELECT id FROM %s WHERE " % table) + ' AND '.join(where)
    cr.execute(query, data)
    record = cr.fetchone()
    if not record:
        return None

    res_id = record[0]

    if data_id:
        cr.execute("""UPDATE ir_model_data
                         SET res_id=%s
                       WHERE id=%s
                   """, (res_id, data_id))
    else:
        cr.execute("""INSERT INTO ir_model_data
                                  (module, name, model, res_id, noupdate)
                           VALUES (%s, %s, %s, %s, %s)
                   """, (module, name, model, res_id, True))

    return res_id


def remove_module(cr, module):
    """ Uninstall the module and delete references to it
       Ensure to reassign records before calling this method
    """
    # NOTE: we cannot use the uninstall of module because the given
    # module need to be currenctly installed and running as deletions
    # are made using orm.

    cr.execute("SELECT id FROM ir_module_module WHERE name=%s", (module,))
    mod_id, = cr.fetchone() or [None]
    if not mod_id:
        return

    # delete constraints only owned by this module
    cr.execute("""SELECT name
                    FROM ir_model_constraint
                GROUP BY name
                  HAVING array_agg(module) = %s""", ([mod_id],))

    constraints = tuple(map(itemgetter(0), cr.fetchall()))
    if constraints:
        cr.execute("""SELECT table_name, constraint_name
                        FROM information_schema.table_constraints
                       WHERE constraint_name IN %s""", (constraints,))
        for table, constraint in cr.fetchall():
            cr.execute('ALTER TABLE "%s" DROP CONSTRAINT "%s"' % (table, constraint))

    cr.execute("""DELETE FROM ir_model_constraint
                        WHERE module=%s
               """, (mod_id,))

    # delete data
    model_ids = tuple()
    cr.execute("""SELECT model, array_agg(res_id)
                    FROM ir_model_data
                GROUP BY model
                  HAVING array_agg(module) = ARRAY[%s::varchar]
               """, (module,))
    for model, res_ids in cr.fetchall():
        if model == 'ir.model':
            model_ids = tuple(res_ids)
        else:
            cr.execute('DELETE FROM "%s" WHERE id IN %%s' % table_of_model(model), (tuple(res_ids),))

    # remove relations
    cr.execute("""SELECT name
                    FROM ir_model_relation
                GROUP BY name
                  HAVING array_agg(module) = %s""", ([mod_id],))
    relations = tuple(map(itemgetter(0), cr.fetchall()))
    cr.execute("DELETE FROM ir_model_relation WHERE module=%s", (mod_id,))
    if relations:
        cr.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN %s", (relations,))
        for rel, in cr.fetchall():
            cr.execute('DROP TABLE "%s" CASCADE' % (rel,))

    if model_ids:
        cr.execute("DELETE FROM ir_model WHERE id IN %s", (model_ids,))

    cr.execute("DELETE FROM ir_model_data WHERE module=%s", (module,))
    cr.execute("DELETE FROM ir_module_module WHERE name=%s", (module,))
    cr.execute("DELETE FROM ir_module_module_dependency WHERE name=%s", (module,))

def rename_module(cr, old, new):
    cr.execute("UPDATE ir_module_module SET name=%s WHERE name=%s", (new, old))
    cr.execute("UPDATE ir_module_module_dependency SET name=%s WHERE name=%s", (new, old))
    cr.execute("UPDATE ir_model_data SET module=%s WHERE module=%s", (new, old))

def force_install_module(cr, module, if_installed=None):
    subquery = ""
    subparams = ()
    if if_installed:
        subquery = """AND EXISTS(SELECT 1 FROM ir_module_module
                                  WHERE name IN %s
                                    AND state IN %s)"""
        subparams = (tuple(if_installed), ('to install', 'to upgrade'))

    cr.execute("""UPDATE ir_module_module
                     SET state=CASE
                                 WHEN state = %s
                                   THEN %s
                                 WHEN state = %s
                                   THEN %s
                                 ELSE state
                               END
                   WHERE name=%s
               """ + subquery + """
               RETURNING state
               """, ('to remove', 'to upgrade',
                     'uninstalled', 'to install',
                     module) + subparams)

    state, = cr.fetchone() or [None]
    return state

def new_module_dep(cr, module, new_dep):
    # One new dep at a time
    # Update new_dep state depending of module state

    states_mod = ('installed', 'to install', 'to upgrade')

    cr.execute("""UPDATE ir_module_module
                     SET state=CASE
                                 WHEN state = %s
                                   THEN %s
                                 WHEN state = %s
                                   THEN %s
                                 ELSE state
                               END
                   WHERE name=%s
                     AND EXISTS(SELECT id
                                  FROM ir_module_module
                                 WHERE name=%s
                                   AND state IN %s
                                )
               """, ('to remove', 'to upgrade',
                     'uninstalled', 'to install',
                     new_dep, module, states_mod))

    cr.execute("""INSERT INTO ir_module_module_dependency(name, module_id)
                       SELECT %s, id
                         FROM ir_module_module m
                        WHERE name=%s
                          AND NOT EXISTS(SELECT 1
                                           FROM ir_module_module_dependency
                                          WHERE module_id = m.id
                                            AND name=%s)
                """, (new_dep, module, new_dep))

def remove_module_deps(cr, module, old_deps):
    assert isinstance(old_deps, tuple)
    cr.execute("""DELETE FROM ir_module_module_dependency
                        WHERE module_id = (SELECT id
                                             FROM ir_module_module
                                            WHERE name=%s)
                          AND name IN %s
               """, (module, old_deps))

def new_module(cr, module, auto_install_deps=None):
    if auto_install_deps:
        cr.execute("""SELECT count(1)
                        FROM ir_module_module
                       WHERE name IN %s
                         AND state IN %s
                   """, (auto_install_deps, ('to install', 'to upgrade')))

        state = 'to install' if cr.fetchone()[0] == len(auto_install_deps) else 'uninstalled'
    else:
        state = 'uninstalled'
    cr.execute("INSERT INTO ir_module_module(name, state) VALUES (%s, %s)", (module, state))

def column_exists(cr, table, column):
    return column_type(cr, table, column) is not None

def column_type(cr, table, column):
    cr.execute("""SELECT udt_name
                    FROM information_schema.columns
                   WHERE table_name = %s
                     AND column_name = %s
               """, (table, column))

    r = cr.fetchone()
    return r[0] if r else None

def create_column(cr, table, column, definition):
    curtype = column_type(cr, table, column)
    if curtype:
        # TODO compare with definition
        pass
    else:
        cr.execute("""ALTER TABLE "%s" ADD COLUMN "%s" %s""" % (table, column, definition))

def table_exists(cr, table):
    cr.execute("""SELECT 1
                    FROM information_schema.tables
                   WHERE table_name = %s
                     AND table_type = 'BASE TABLE'
               """, (table,))
    return cr.fetchone() is not None

def remove_field(cr, model, fieldname):
    cr.execute("DELETE FROM ir_model_fields WHERE model=%s AND name=%s RETURNING id", (model, fieldname))
    fids = tuple(map(itemgetter(0), cr.fetchall()))
    if fids:
        cr.execute("DELETE FROM ir_model_data WHERE model=%s AND res_id IN %s", ('ir.model.fields', fids))
    table = table_of_model(cr, model)
    if column_exists(cr, table, fieldname):
        cr.execute('ALTER TABLE "{0}" DROP COLUMN "{1}"'.format(table, fieldname))

def rename_field(cr, model, old, new):
    cr.execute("UPDATE ir_model_fields SET name=%s WHERE model=%s AND name=%s RETURNING id", (model, new, old))
    [fid] = cr.fetchone()
    if fid:
        name = 'field_%s_%s' % (model.replace('.', '_'), new)
        cr.execute("UPDATE ir_model_data SET name=%s WHERE model=%s AND res_id=%s", (name, 'ir.model.fields', fid))
    table = table_of_model(cr, model)
    if column_exists(cr, table, old):
        cr.execute('ALTER TABLE "{0}" ALTER COLUMN "{1}" RENAME TO "{2}"'.format(table, old, new))


def res_model_res_id(cr, filtered=True):
    each = [
        ('ir.attachment', 'res_model', 'res_id'),
        ('ir.cron', 'model', None),
        ('ir.actions.report.xml', 'model', None),
        ('ir.actions.act_window', 'res_model', 'res_id'),
        ('ir.actions.act_window', 'src_model', None),
        ('ir.actions.server', 'wkf_model_name', None),   # stored related, also need to be updated
        ('ir.actions.server', 'crud_model_name', None),  # idem
        ('ir.actions.client', 'res_model', None),
        ('ir.model', 'model', None),
        ('ir.model.fields', 'model', None),
        ('ir.model.data', 'model', 'res_id'),
        ('ir.filters', 'model_id', None),     # YUCK!, not an id
        ('ir.ui.view', 'model', None),
        ('ir.values', 'model', 'res_id'),
        ('workflow.transition', 'trigger_model', None),
        ('workflow_triggers', 'model', None),

        ('ir.model.fields.anonymization', 'model_name', None),
        ('ir.model.fields.anonymization.migration.fix', 'model_name', None),
        ('base_import.import', 'res_model', None),
        ('email.template', 'model', None),      # stored related
#        ('mail.alias', 'alias_model_id.model', 'alias_force_thread_id'),
#        ('mail.alias', 'alias_parent_model_id.model', 'alias_parent_thread_id'),
        ('mail.followers', 'res_model', 'res_id'),
        ('mail.message.subtype', 'res_model', None),
        ('mail.message', 'model', 'res_id'),
        ('mail.wizard.invite', 'res_model', 'res_id'),
        ('mail.mail.statistics', 'model', 'res_id'),
        ('project.project', 'alias_model', None),
    ]

    for model, res_model, res_id in each:
        if filtered:
            table = table_of_model(cr, model)
            if not column_exists(cr, table, res_model):
                continue
            if res_id and not column_exists(cr, table, res_id):
                continue

        yield model, res_model, res_id

def delete_model(cr, model, drop_table=True):
    model_underscore = model.replace('.', '_')
    cr.execute("SELECT id FROM ir_model WHERE model=%s", (model,))
    [mod_id] = cr.fetchone() or [None]
    if mod_id:
        cr.execute("DELETE FROM ir_model_constraint WHERE model=%s", (mod_id,))
        cr.execute("DELETE FROM ir_model_relation WHERE model=%s", (mod_id,))
    cr.execute("DELETE FROM ir_model WHERE model=%s", (model,))
    cr.execute("DELETE FROM ir_model_data WHERE model=%s", (model,))
    cr.execute("DELETE FROM ir_model_data WHERE model=%s AND name=%s",
               ('ir.model', 'model_%s' % model_underscore))
    cr.execute("DELETE FROM ir_model_data WHERE model=%s AND name like %s",
               ('ir.model.fields', 'field_%s_%%' % model_underscore))

    if drop_table:
        cr.execute('DROP TABLE "{0}" CASCADE'.format(table_of_model(cr, model)))

def rename_model(cr, old, new, rename_table=True, module=None):
    if rename_table:
        old_table = table_of_model(cr, old)
        new_table = table_of_model(cr, new)
        cr.execute('ALTER TABLE "{0}" RENAME TO "{1}"'.format(old_table, new_table))
        cr.execute('ALTER SEQUENCE "{0}_id_seq" RENAME TO "{1}_id_seq"'.format(old_table, new_table))
        cr.execute('ALTER INDEX "{0}_pkey" RENAME TO "{1}_pkey"'.format(old_table, new_table))

        # DELETE all constraints and indexes (ignore the PK), ORM will recreate them.
        cr.execute("""SELECT constraint_name
                        FROM information_schema.table_constraints
                       WHERE table_name=%s
                         AND constraint_type!=%s
                         AND constraint_name !~ '^[0-9_]+_not_null$'
                   """, (new_table, 'PRIMARY KEY'))
        for const, in cr.fetchall():
            cr.execute("DELETE FROM ir_model_constraint WHERE name=%s", (const,))
            cr.execute('ALTER TABLE "{0}" DROP CONSTRAINT "{1}"'.format(new_table, const))

    updates = [('wkf', 'osv')] + [r[:2] for r in res_model_res_id(cr)]

    for model, column in updates:
        table = table_of_model(cr, model)
        query = 'UPDATE {t} SET {c}=%s WHERE {c}=%s'.format(t=table, c=column)
        cr.execute(query, (new, old))

    cr.execute("SELECT model, name FROM ir_model_fields WHERE ttype=%s", ('reference',))
    for model, column in cr.fetchall():
        table = table_of_model(cr, model)
        if column_exists(cr, table, column):
            cr.execute("""UPDATE "{table}"
                             SET {column}='{new}' || substring({column} FROM '%#",%#"' FOR '#')
                           WHERE {column} LIKE '{old},%'
                       """.format(table=table, column=column, new=new, old=old))

    old_u = old.replace('.', '_')
    new_u = new.replace('.', '_')

    mod_reassign_query = ""
    mod_reassign_data = ()
    if module:
        mod_reassign_query = ", module=%s "
        mod_reassign_data = (module,)

    cr.execute("UPDATE ir_model_data SET name=%s" + mod_reassign_query + " WHERE model=%s AND name=%s",
               ('model_%s' % new_u,) + mod_reassign_data + ('ir.model', 'model_%s' % old_u))

    cr.execute("""UPDATE ir_model_data
                     SET name=%%s || substring(name from %%s)
                         %s
                   WHERE model=%%s
                     AND name LIKE %%s
               """ % mod_reassign_query,
               ('field_%s_' % new_u, len(old_u) + 7) + mod_reassign_data + ('ir.model.fields', 'field_%s_%%' % old_u))

def replace_record_references(cr, old, new):
    """replace all indirect references of a record to another"""
    # TODO update workflow instances?
    assert isinstance(old, tuple) and len(old) == 2
    assert isinstance(new, tuple) and len(new) == 2

    for model, res_model, res_id in res_model_res_id(cr):
        if not res_id:
            continue
        table = table_of_model(cr, model)
        cr.execute("""UPDATE {table}
                         SET {res_model}=%s, {res_id}=%s
                       WHERE {res_model}=%s
                         AND {res_id}=%s
                   """.format(table=table, res_model=res_model, res_id=res_id),
                   new + old)

    comma_new = '%s,%d' % new
    comma_old = '%s,%d' % old
    cr.execute("SELECT model, name FROM ir_model_fields WHERE ttype=%s", ('reference',))
    for model, column in cr.fetchall():
        table = table_of_model(cr, model)
        if column_exists(cr, table, column):
            cr.execute("""UPDATE "{table}"
                             SET "{column}"=%s
                           WHERE "{column}"=%s
                       """.format(table=table, column=column),
                       (comma_new, comma_old))


def rst2html(rst):
    overrides = dict(embed_stylesheet=False, doctitle_xform=False, output_encoding='unicode', xml_declaration=False)
    html = publish_string(source=dedent(rst), settings_overrides=overrides, writer=MyWriter())
    return html_sanitize(html, silent=False)


_DEFAULT_HEADER = """
<p>OpenERP has been upgraded to version {version}.</p>
<h2>What's new in this upgrade?</h2>
""".format(version=release.version)

_DEFAULT_FOOTER = "<p>Enjoy the new OpenERP Online!</p>"

def announce(cr, msg, format='rst', header=_DEFAULT_HEADER, footer=_DEFAULT_FOOTER):
    registry = RegistryManager.get(cr.dbname)
    IMD = registry['ir.model.data']
    user = registry['res.users'].browse(cr, SUPERUSER_ID, SUPERUSER_ID)
    try:
        poster = IMD.get_object(cr, SUPERUSER_ID, 'mail', 'group_all_employees')
    except ValueError:
        # Cannot found group, post the message on the wall of the admin
        poster = user

    if not poster.exists():
        return

    if format == 'rst':
        msg = rst2html(msg)

    message = (header or "") + msg + (footer or "")
    _logger.debug(message)

    try:
        poster.message_post(message, partner_ids=[user.partner_id.id], type='notification', subtype='mail.mt_comment')
    except Exception:
        _logger.warning('Cannot annouce new version', exc_info=True)
