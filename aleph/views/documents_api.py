import os
import logging
from werkzeug.exceptions import BadRequest, NotFound
from flask import Blueprint, redirect, send_file, request
from apikit import jsonify, Pager, request_data

from aleph.core import archive, url_for, db
from aleph.model import Document, DocumentRecord, Entity, Reference
from aleph.logic import update_document
from aleph.events import log_event
from aleph.views.cache import enable_cache
from aleph.search import QueryState
from aleph.search import records_query, execute_records_query
from aleph.search.util import next_params
from aleph.views.util import get_document


log = logging.getLogger(__name__)
blueprint = Blueprint('documents_api', __name__)


@blueprint.route('/api/1/documents', methods=['GET'])
def index():
    authz = request.authz
    collections = request.args.getlist('collection')
    collections = authz.collections_intersect(authz.READ, collections)
    q = Document.all()
    q = q.filter(Document.collection_id.in_(collections))
    hashes = request.args.getlist('content_hash')
    if len(hashes):
        q = q.filter(Document.content_hash.in_(hashes))
    return jsonify(Pager(q))


@blueprint.route('/api/1/documents/<int:document_id>')
def view(document_id):
    doc = get_document(document_id)
    enable_cache()
    data = doc.to_dict()
    log_event(request, document_id=doc.id)
    data['data_url'] = archive.generate_url(doc.meta)
    if data['data_url'] is None:
        data['data_url'] = url_for('documents_api.file',
                                   document_id=document_id)
    if doc.meta.is_pdf:
        data['pdf_url'] = data['data_url']
    else:
        try:
            data['pdf_url'] = archive.generate_url(doc.meta.pdf)
        except Exception as ex:
            log.info('Could not generate PDF url: %r', ex)
        if data.get('pdf_url') is None:
            data['pdf_url'] = url_for('documents_api.pdf',
                                      document_id=document_id)
    return jsonify(data)


@blueprint.route('/api/1/documents/<int:document_id>', methods=['POST', 'PUT'])
def update(document_id):
    document = get_document(document_id, action=request.authz.WRITE)
    data = request_data()
    document.update(data)
    db.session.commit()
    log_event(request, document_id=document.id)
    update_document(document)
    return view(document_id)


@blueprint.route('/api/1/documents/<int:document_id>/references')
def references(document_id):
    doc = get_document(document_id)
    q = db.session.query(Reference)
    q = q.filter(Reference.document_id == doc.id)
    q = q.filter(Reference.origin == 'regex')
    q = q.join(Entity)
    q = q.filter(Entity.state == Entity.STATE_ACTIVE)
    q = q.filter(Entity.collection_id.in_(request.authz.collections_read))
    q = q.order_by(Reference.weight.desc())
    return jsonify(Pager(q, document_id=document_id))


@blueprint.route('/api/1/documents/<int:document_id>/file')
def file(document_id):
    document = get_document(document_id)
    enable_cache(server_side=True)
    log_event(request, document_id=document.id)
    url = archive.generate_url(document.meta)
    if url is not None:
        return redirect(url)

    local_path = archive.load_file(document.meta)
    if not os.path.isfile(local_path):
        raise NotFound("File does not exist.")

    fh = open(local_path, 'rb')
    return send_file(fh, as_attachment=True,
                     attachment_filename=document.meta.file_name,
                     mimetype=document.meta.mime_type)


@blueprint.route('/api/1/documents/<int:document_id>/pdf')
def pdf(document_id):
    document = get_document(document_id)
    enable_cache(server_side=True)
    log_event(request, document_id=document.id)
    if document.type != Document.TYPE_TEXT:
        raise BadRequest("PDF is only available for text documents")
    pdf = document.meta.pdf
    url = archive.generate_url(pdf)
    if url is not None:
        return redirect(url)

    try:
        local_path = archive.load_file(pdf)
        fh = open(local_path, 'rb')
    except Exception as ex:
        raise NotFound("Missing PDF file: %r" % ex)
    return send_file(fh, mimetype=pdf.mime_type)


@blueprint.route('/api/1/documents/<int:document_id>/tables/<int:table_id>')
def table(document_id, table_id):
    document = get_document(document_id)
    enable_cache(vary_user=True)
    try:
        return jsonify(document.meta.tables[table_id])
    except IndexError:
        raise NotFound("No such table: %s" % table_id)


@blueprint.route('/api/1/documents/<int:document_id>/records')
def records(document_id):
    document = get_document(document_id)
    enable_cache(vary_user=True)
    state = QueryState(request.args, request.authz)
    query = records_query(document.id, state)
    result = execute_records_query(document.id, state, query)
    params = next_params(request.args, result)
    if params is not None:
        result['next'] = url_for('documents_api.records',
                                 document_id=document_id,
                                 **params)
    return jsonify(result)


@blueprint.route('/api/1/documents/<int:document_id>/records/<int:index>')
def record(document_id, index):
    document = get_document(document_id)
    q = db.session.query(DocumentRecord)
    q = q.filter(DocumentRecord.document_id == document.id)
    q = q.filter(DocumentRecord.index == index)
    record = q.first()
    if record is None:
        raise NotFound("No such page: %s" % index)
    enable_cache(server_side=True)
    return jsonify(record)
