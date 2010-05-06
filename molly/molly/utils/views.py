from inspect import isfunction
import simplejson, logging
from datetime import datetime, date
from xml.etree import ElementTree as ET

from django.db import models
from django.db.models.query import QuerySet
from django.http import HttpResponse, HttpResponseNotAllowed
from django.template import TemplateDoesNotExist, RequestContext
from django.shortcuts import render_to_response
from django.core.paginator import Paginator
from django.contrib.gis.geos import Point

logger = logging.getLogger('core.requests')

class DateUnicode(unicode): pass

def renderer(format, mimetypes=None):
    """
    Decorates a view method to say that it renders a particular format and mimetypes.

    Use as:
        @renderer(format="foo")
        def render_foo(cls, request, context, template_name): ...
    or
        @renderer(format="foo", mimetypes=("application/x-foo",))
        def render_foo(cls, request, context, template_name): ...

    The former case will inherit mimetypes from the previous renderer for that
    format in the MRO. Where there isn't one, it will default to the empty
    tuple.
    """

    def g(f):
        f.is_renderer = True
        f.format = format
        f.mimetypes = mimetypes
        return f
    return g

class ViewMetaclass(type):
    def __new__(cls, name, bases, dict):

        # Pull the renderers from the bases into a couple of new dicts for
        # this views renderers
        formats_by_mimetype = {}
        formats = {}
        for base in reversed(bases):
            if hasattr(base, 'FORMATS'):
                formats.update(base.FORMATS)
                formats_by_mimetype.update(base.FORMATS_BY_MIMETYPE)

        for key, value in dict.items():
            # Wrap all methods in classmethods so they don't need decorating
            # individually.
            if isfunction(value) and key != '__new__':
                dict[key] = classmethod(value)

            # If the method is a renderer we add it to our dicts. We can't add
            # the functions right now because we want them bound to the class
            # object that hasn't yet been created. Instead, add the keys (strs)
            # and we'll replace them with the bound classmethods later.
            if isfunction(value) and getattr(value, 'is_renderer', False):
                if value.mimetypes is not None:
                    mimetypes = value.mimetypes
                elif value.format in formats:
                    mimetypes = formats[value.format].mimetypes
                else:
                    mimetypes = ()
                for mimetype in mimetypes:
                    formats_by_mimetype[mimetype] = key
                formats[value.format] = key

        dict.update({
            'FORMATS': formats,
            'FORMATS_BY_MIMETYPE': formats_by_mimetype,
        })

        # Create our view.
        view = type.__new__(cls, name, bases, dict)

        # Replace those that items that have string values with the bound
        # classmethods we wanted in the first place.
        for format in view.FORMATS:
            if isinstance(view.FORMATS[format], basestring):
                view.FORMATS[format] = getattr(view, view.FORMATS[format])
        for mimetype in view.FORMATS_BY_MIMETYPE:
            if isinstance(view.FORMATS_BY_MIMETYPE[mimetype], basestring):
                view.FORMATS_BY_MIMETYPE[mimetype] = getattr(view, view.FORMATS_BY_MIMETYPE[mimetype])

        return view

class BaseView(object):
    __metaclass__ = ViewMetaclass

    ALLOWABLE_METHODS = ('GET', 'POST', 'DELETE', 'HEAD', 'OPTIONS', 'PUT')

    def method_not_allowed(cls, request):
        return HttpResponseNotAllowed([m for m in cls.ALLOWABLE_METHODS if hasattr(cls, 'handle_%s' % m)])

    def not_acceptable(cls, request):
        response = HttpResponse("The desired media type is not supported for this resource.", mimetype="text/plain")
        response.status_code = 406
        return response

    def bad_request(cls, request):
        response = HttpResponse(
            'Your request was malformed.',
            status=400,
        )
        return response

    def initial_context(cls, request, *args, **kwargs):
        return {}

    def __new__(cls, request, *args, **kwargs):
        method_name = 'handle_%s' % request.method
        if hasattr(cls, method_name):
            context = cls.initial_context(request, *args, **kwargs)
            context['breadcrumbs'] = cls.breadcrumb(request, context, *args, **kwargs)
            response = getattr(cls, method_name)(request, context, *args, **kwargs)
            return response
        else:
            return cls.method_not_allowed(request)

    def handle_HEAD(cls, request, *args, **kwargs):
        """
        Provides a default HEAD handler that strips the content from the
        response returned by the GET handler.
        """
        if hasattr(cls, 'handle_GET'):
            response = cls.handle_GET(request, *args, **kwargs)
        else:
            response = cls.method_not_acceptable(request)
        response.content = ''
        return response

    def get_zoom(cls, request, default=16):
        try:
            zoom = int(request.GET['zoom'])
        except (ValueError, KeyError):
            zoom = default
        else:
            zoom = min(max(10, zoom), 18)
        return zoom

    def render(cls, request, context, template_name):
        if request.GET.get('format') in cls.FORMATS:
            renderer = cls.FORMATS[request.GET['format']]
        elif 'format' in request.GET:
            return cls.not_acceptable(request)
        elif request.is_ajax():
            renderer = cls.FORMATS['json']
        elif request.META.get('HTTP_ACCEPT'):
            accepts = [a.split(';')[0].strip() for a in request.META['HTTP_ACCEPT'].split(',')]
            for accept in accepts:
                # WebKit's Accept header is broken. See
                # http://www.newmediacampaigns.com/page/webkit-team-admits-accept-header-error
                # and https://bugs.webkit.org/show_bug.cgi?id=27267
                if accept == 'application/xml' and ' AppleWebKit/' in request.META.get('HTTP_USER_AGENT', ''):
                    continue
                if accept in cls.FORMATS_BY_MIMETYPE:
                    renderer = cls.FORMATS_BY_MIMETYPE[accept]
                    try:
                        return renderer(request, context, template_name)
                    except NotImplementedError:
                        pass
            else:
                response = HttpResponse("""\
Your Accept header didn't contain any supported media ranges.

Supported ranges are:

 * %s\n""" % '\n * '.join(f for f in cls.FORMATS), mimetype="text/plain" )
                response.status_code = 406 # Not Acceptable
                return response
        else:
            renderer = cls.FORMATS['html']

        try:
            return renderer(request, context, template_name)
        except NotImplementedError:
            return cls.not_acceptable(request)


    def render_to_format(cls, request, context, template_name, format):
        render_method = cls.FORMATS[format]
        return render_method(request, context, template_name)

    @renderer(format="json", mimetypes=('application/json',))
    def render_json(cls, request, context, template_name):
        context = cls.simplify_value(context)
        return HttpResponse(simplejson.dumps(context), mimetype="application/json")

    @renderer(format="html", mimetypes=('text/html', 'application/xhtml+xml', '*/*'))
    def render_html(cls, request, context, template_name):
        if template_name is None:
            raise NotImplementedError
        return render_to_response(template_name+'.html',
                                  context, context_instance=RequestContext(request),
                                  mimetype='text/html')

    @renderer(format="xml", mimetypes=('application/xml', 'text/xml'))
    def render_xml(cls, request, context, template_name):
        context = cls.simplify_value(context)
        return HttpResponse(ET.tostring(cls.serialize_to_xml(context)), mimetype="application/xml")

    @renderer(format="yaml", mimetypes=('application/x-yaml',))
    def render_yaml(cls, request, context, template_name):
        try:
            import yaml
        except ImportError:
            raise NotImplementedError

        context = cls.simplify_value(context)
        return HttpResponse(yaml.safe_dump(context), mimetype="application/x-yaml")

    def simplify_value(cls, value):
        if hasattr(value, 'simplify_for_render'):
            return value.simplify_for_render(cls.simplify_value, cls.simplify_model)
        elif isinstance(value, dict):
            out = {}
            for key in value:
                try:
                    out[key] = cls.simplify_value(value[key])
                except NotImplementedError:
                    pass
            return out
        elif isinstance(value, (list, tuple, set, frozenset)):
            out = []
            for subvalue in value:
                try:
                    out.append(cls.simplify_value(subvalue))
                except NotImplementedError:
                    print "Problem", type(subvalue)
                    pass
            if isinstance(value, tuple):
                return tuple(out)
            else:
                return out
        elif isinstance(value, (basestring, int, float)):
            return value
        elif isinstance(value, (datetime, date)):
            return DateUnicode(context.isoformat(' '))
        elif hasattr(type(value), '__mro__') and models.Model in type(value).__mro__:
            return cls.simplify_model(value)
        elif isinstance(value, Paginator):
            return cls.simplify_value(value.object_list)
        elif value is None:
            return None
        elif hasattr(value, '__iter__'):
            return [cls.simplify_value(item) for item in value]
        elif isinstance(value, Point):
            return cls.simplify_value(list(value))
        else:
            print "Couldn't simplify", type(value)
            raise NotImplementedError

    XML_DATATYPES = (
        (DateUnicode, 'datetime'),
        (str, 'string'),
        (unicode, 'string'),
        (int, 'integer'),
        (float, 'float'),
    )
    
    def simplify_model(cls, obj, terse=False):
        if obj is None:
            return None
        # It's a Model instance
        if hasattr(obj._meta, 'expose_fields'):
            expose_fields = obj._meta.expose_fields
        else:
            expose_fields = [f.name for f in obj._meta.fields]
        out = {
            '_type': '%s.%s' % (obj.__module__[:-7], obj._meta.object_name),
            '_pk': obj.pk,
        }
        print "EEE", type(obj), hasattr(obj, 'get_absolute_url') 
        if hasattr(obj, 'get_absolute_url'):
            out['_url'] = obj.get_absolute_url()
        if terse:
            out['_terse'] = True
        else:
            for field_name in expose_fields:
                if field_name in ('password',):
                    continue
                try:
                    value = getattr(obj, field_name)
                    if hasattr(type(value), '__bases__') and models.Model in type(value).__bases__:
                        value = cls.simplify_model(value, terse=True)
                    out[field_name] = cls.simplify_value(value)
                except NotImplementedError:
                    pass
        return out

    def serialize_to_xml(cls, value):
        if value is None:
            node = ET.Element('null')
        elif isinstance(value, bool):
            node = ET.Element('literal')
            node.text = 'true' if value else 'false'
            node.attrib['type'] = 'boolean'
        elif isinstance(value, (basestring, int, float)):
            node = ET.Element('literal')
            node.text = unicode(value)
            node.attrib['type'] = [d[1] for d in cls.XML_DATATYPES if isinstance(value, d[0])][0]
        elif isinstance(value, dict):
            if '_type' in value:
                node = ET.Element('object', {'type': value['_type'], 'pk': unicode(value.get('_pk', ''))})
                del value['_type']
                del value['_pk']
                if '_url' in value:
                    node.attrib['url'] = value['_url']
                    del value['_url']
                if value.get('_terse'):
                    node.attrib['terse'] = 'true'
                    del value['_terse']
            else:
                node = ET.Element('collection', {'type': 'mapping'})
            for key in value:
                v = cls.serialize_to_xml(value[key])
                subnode = ET.Element('item', {'key':key})
                subnode.append(v)
                node.append(subnode)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for x,y in ((list, 'list'), (tuple, 'tuple')):
                if isinstance(value, x):
                    node = ET.Element('collection', {'type': y})
                    break
            else:
                node = ET.Element('collection', {'type':'set'})
            for item in value:
                v = cls.serialize_to_xml(item)
                subnode = ET.Element('item')
                subnode.append(v)
                node.append(subnode)
        else:
            node = ET.Element('unknown')

        return node


class ZoomableView(BaseView):
    default_zoom = None

    def initial_context(cls, request, *args, **kwargs):
        try:
            zoom = int(request.GET['zoom'])
        except (KeyError, ValueError):
            zoom = cls.default_zoom
        else:
            zoom = min(max(10, zoom), 18)
        return {
            'zoom': zoom,
        }

class SecureView(BaseView):
    pass
