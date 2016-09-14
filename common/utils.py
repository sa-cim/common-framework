# coding: utf-8
import abc
import collections
import inspect
import json
import logging
import mimetypes
import os
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import wraps
from importlib import import_module
from itertools import chain, product
from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.files import temp
from django.core.files.uploadedfile import TemporaryUploadedFile
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from django.db.models import ForeignKey, OneToOneField
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from rest_framework.utils.encoders import JSONEncoder


# Logging
logger = logging.getLogger(__name__)


class singleton:
    """
    Décorateur pour définir une classe singleton
    """

    def __init__(self, _class):
        self._class = _class
        self.instance = None

    def __call__(self, *args, **kwargs):
        if self.instance is None:
            self.instance = self._class(*args, **kwargs)
        return self.instance


@singleton
class CeleryFake:
    """
    Mock Celery pour les tâches asynchrones
    """
    def task(self, *a, **k):
        def decorator(func):
            @wraps(func)
            def wrapped(*args, **kwargs):
                return func(*args, **kwargs)

            func.apply = lambda args=None, kwargs=None, **options: func(*(args or []), **(kwargs or {}))
            func.apply_async = func.apply
            return wrapped
        return decorator


def get_current_app():
    """
    Récupère l'application Celery actuelle ou un mock
    :return: Application Celery ou mock
    """
    try:
        from celery.app import current_app
        return current_app()
    except ImportError:
        return CeleryFake()


def parsedate(input_date, start_day=False, end_day=False, date_only=False, utc=False, **kwargs):
    """
    Permet de parser une date quelconque (chaîne, date ou datetime) en un datetime standardisée avec time zone
    :param input_date: Date quelconque
    :param start_day: Ajoute 00:00:00.000000 à une date sans heure (exclusif avec end_day)
    :param end_day: Ajoute 23:59:59.999999 à une date sans heure (exclusif avec start_day)
    :param date_only: Retourne uniquement la date sans l'heure
    :param utc: Retourne la date uniquement en UTC
    :return: Aware datetime ou date
    """
    _date = input_date
    if not _date:
        return None
    if isinstance(_date, (date, datetime)):
        if date_only:
            return _date
        if not start_day and not end_day:
            start_day = True
    elif not isinstance(_date, datetime):
        try:
            from dateutil import parser
            _date = parser.parse(_date, **kwargs)
        except (ImportError, ValueError, OverflowError):
            return None
    if date_only:
        return _date.date()
    if start_day ^ end_day:
        _time = time.min if start_day else time.max
        _date = datetime.combine(_date, _time)
    try:
        import pytz
        use_tz = getattr(settings, 'USE_TZ', None)
        timezone = getattr(settings, 'TIME_ZONE', None)
        if utc or not use_tz:
            timezone = pytz.utc
        elif timezone:
            timezone = pytz.timezone(timezone)
        if not timezone:
            return _date
        if _date.tzinfo:
            return _date.astimezone(timezone)
        return timezone.localize(_date)
    except ImportError:
        return _date


def timeit(name, log=logger.info):
    """
    Decorateur pour évaluer le temps d'exécution d'une méthode
    :param name: Nom lisible de la méthode
    :param log: Logger
    :return: Decorateur
    """
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            ts = datetime.now()
            log(_("[{}] démarré...").format(name))
            try:
                result = func(*args, **kwargs)
            except Exception as error:
                log(_("[{}] en échec : {}").format(name, error))
                raise
            te = datetime.now()
            log(_("[{}] terminé en {} !").format(name, te - ts))
            return result
        return wrapped
    return decorator


def synchronized(lock=None):
    """
    Décorateur permettant de verrouiller l'accès simultané à une méthode
    :param lock: Verrou externe partagé
    :return: Decorateur
    """
    def decorator(func):
        func.__lock__ = lock or threading.Lock()

        @wraps(func)
        def wrapped(*args, **kwargs):
            with func.__lock__:
                return func(*args, **kwargs)
        return wrapped
    return decorator


class TemporaryFile(TemporaryUploadedFile):
    """
    Fichier temporaire avec conservation du nom d'origine
    """

    def __init__(self, name, content_type, size, charset, content_type_extra=None):
        file = temp.NamedTemporaryFile(suffix='.' + name)
        super(TemporaryUploadedFile, self).__init__(file, name, content_type, size, charset, content_type_extra)


class TemporaryFileHandler(TemporaryFileUploadHandler):
    """
    Gestionnaire d'upload de fichier temporaire avec conservation du nom d'origine
    """

    def new_file(self, file_name, *args, **kwargs):
        super(TemporaryFileUploadHandler, self).new_file(file_name, *args, **kwargs)
        self.file = TemporaryFile(self.file_name, self.content_type, 0, self.charset, self.content_type_extra)


def temporary_upload(function):
    """
    Décorateur permettant d'indiquer que la vue utilisera l'import de fichier temporaire dans son traitement
    :param function: Méthode à décorer
    :return: Méthode décorée
    """
    @wraps(function)
    @csrf_exempt
    def wrapped(request, *args, **kwargs):
        request.upload_handlers = [TemporaryFileHandler()]
        return csrf_protect(function)(request, *args, **kwargs)
    return wrapped


# Objet permettant de définir un fichier à télécharger
# file : fichier, name : nom du fichier à télécharge, delete : supprimer le fichier après le téléchargement
DownloadFile = collections.namedtuple('DownloadFile', ['file', 'name', 'delete'])


def download_file(function):
    """
    Décorateur permettant de proposer le téléchargement d'un fichier à partir d'une fonction
    La fonction à décorer doit retourner une instance de DownloadFile
    :param function: Méthode à décorer
    :return: Méthode décorée
    """
    def wrapper(*args, **kwargs):
        file = function(*args, **kwargs)
        if isinstance(file, DownloadFile):
            from wsgiref.util import FileWrapper
            file_wrapper = FileWrapper(file.file)
            filename, extension = os.path.splitext(file.name)
            response = HttpResponse(file_wrapper, content_type=mimetypes.types_map.get(extension))
            response["Content-Disposition"] = "attachment; filename={0}".format(file.name)
            file.file.close()
            if file.delete:
                os.unlink(file.file.name)
            return response
        else:
            return file
    return wrapper


def render_to(template=None, content_type=None):
    """
    Decorator for Django views that sends returned dict to render_to_response
    function.

    Template name can be decorator parameter or TEMPLATE item in returned
    dictionary.  RequestContext always added as context instance.
    If view doesn't return dict then decorator simply returns output.

    Parameters:
     - template: template name to use
     - content_type: content type to send in response headers

    Examples:
    # 1. Template name in decorator parameters

    @render_to('template.html')
    def foo(request):
        bar = Bar.object.all()
        return {'bar': bar}

    # equals to
    def foo(request):
        bar = Bar.object.all()
        return render_to_response('template.html',
                                  {'bar': bar},
                                  context_instance=RequestContext(request))


    # 2. Template name as _template item value in return dictionary.
         if _template is given then its value will have higher priority
         than render_to argument.

    @render_to()
    def foo(request, category):
        template_name = '%s.html' % category
        return {'bar': bar, '_template': template_name}

    #equals to
    def foo(request, category):
        template_name = '%s.html' % category
        return render_to_response(template_name,
                                  {'bar': bar},
                                  context_instance=RequestContext(request))

    """
    def renderer(function):
        @wraps(function)
        def wrapper(request, *args, **kwargs):
            output = function(request, *args, **kwargs)
            if not isinstance(output, dict):
                return output
            tmpl = output.pop('TEMPLATE', template)
            if tmpl is None:
                template_dir = os.path.join(*function.__module__.split('.')[:-1])
                tmpl = os.path.join(template_dir, function.func_name + ".html")
            # Explicit version check to avoid swallowing other exceptions
            return render(request, tmpl, output, content_type=content_type)
        return wrapper
    return renderer


FORMAT_TYPES = {
    'application/json': lambda response: json_encode(response),
    'text/json': lambda response: json_encode(response),
}

try:
    import yaml
    FORMAT_TYPES.update({
        'application/yaml': yaml.dump,
        'text/yaml': yaml.dump,
    })
except ImportError:
    pass


def ajax_request(func):
    """
    If view returned serializable dict, returns response in a format requested
    by HTTP_ACCEPT header. Defaults to JSON if none requested or match.

    Currently supports JSON or YAML (if installed), but can easily be extended.

    example:

        @ajax_request
        def my_view(request):
            news = News.objects.all()
            news_titles = [entry.title for entry in news]
            return {'news_titles': news_titles}
    """
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        for accepted_type in request.META.get('HTTP_ACCEPT', '').split(','):
            if accepted_type in FORMAT_TYPES.keys():
                format_type = accepted_type
                break
        else:
            format_type = 'application/json'
        response = func(request, *args, **kwargs)
        if not isinstance(response, HttpResponse):
            if hasattr(settings, 'FORMAT_TYPES'):
                format_type_handler = settings.FORMAT_TYPES[format_type]
                if hasattr(format_type_handler, '__call__'):
                    data = format_type_handler(response)
                elif isinstance(format_type_handler, str):
                    mod_name, func_name = format_type_handler.rsplit('.', 1)
                    module = __import__(mod_name, fromlist=[func_name])
                    function = getattr(module, func_name)
                    data = function(response)
            else:
                data = FORMAT_TYPES[format_type](response)
            response = HttpResponse(data, content_type=format_type)
            response['Content-Length'] = len(data)
        return response
    return wrapper


# Liste des built-ins considérés comme "sûrs"
SAFE_GLOBALS = dict(__builtins__=dict(
    abs=abs,
    all=all,
    any=any,
    ascii=ascii,
    bin=bin,
    bool=bool,
    bytearray=bytearray,
    bytes=bytes,
    # callable=callable,
    chr=chr,
    # classmethod=classmethod,
    # compile=compile,
    complex=complex,
    delattr=delattr,
    dict=dict,
    # dir=dir,
    divmod=divmod,
    enumerate=enumerate,
    # eval=eval,
    # exec=exec,
    filter=filter,
    float=float,
    format=format,
    frozenset=frozenset,
    getattr=getattr,
    # globals=globals,
    hasattr=hasattr,
    hash=hash,
    help=help,
    hex=hex,
    id=id,
    # input=input,
    int=int,
    # isinstance=isinstance,
    # issubclass=issubclass,
    iter=iter,
    len=len,
    list=list,
    # locals=locals,
    map=map,
    max=max,
    # memoryview=memoryview,
    min=min,
    next=next,
    # object=object,
    oct=oct,
    # open=open,
    ord=ord,
    pow=pow,
    # print=print,
    # property=property,
    range=range,
    repr=repr,
    reversed=reversed,
    round=round,
    set=set,
    setattr=setattr,
    slice=slice,
    sorted=sorted,
    # staticmethod=staticmethod,
    str=str,
    sum=sum,
    # super=super,
    tuple=tuple,
    # type=type,
    # vars=vars,
    zip=zip,
    # __import__=__import__,
))


def evaluate(expression, _globals=None, _locals=None, default=False):
    """
    Evalue une expression Python
    :param expression: Expression
    :param _globals: Contexte global
    :param _locals: Contexte local
    :param default: Comportement par défaut ?
    :return: Résultat de l'évaluation
    """
    if _globals is None:
        _globals = inspect.currentframe().f_back.f_globals.copy()
    if _locals is None:
        _locals = inspect.currentframe().f_back.f_locals.copy()
    if not default:
        _globals.update(SAFE_GLOBALS)
    return eval(expression, _globals, _locals)


def execute(statement, _globals=globals(), _locals=locals(), default=False):
    """
    Exécute un statement Python
    :param statement: Statement
    :param _globals: Contexte global
    :param _locals: Contexte local
    :param default: Comportement par défaut ?
    :return: Rien
    """
    if _globals is None:
        _globals = inspect.currentframe().f_back.f_globals.copy()
    if _locals is None:
        _locals = inspect.currentframe().f_back.f_locals.copy()
    if not default:
        _globals.update(SAFE_GLOBALS)
    exec(statement, _globals, _locals)


@contextmanager
def patch_settings(**kwargs):
    """
    Permet de patcher temporairement les settings Django
    :param kwargs: Valeurs à changer
    :return: Rien
    """
    old_settings = {}
    for key, new_value in kwargs.items():
        old_value = getattr(settings, key, None)
        old_settings[key] = old_value
        setattr(settings, key, new_value)
    yield
    for key, old_value in old_settings.items():
        if old_value is None:
            delattr(settings, key)
        else:
            setattr(settings, key, old_value)


def recursive_dict_product(input_dict, all_keys=None, long_keys=False,
                           separator='_', ignore='*', auto_id='id', prefix=''):
    """
    Retourne le produit de combinaisons d'un dictionnaire (avec listes et dictionnaires imbriqués) en renommant les clés
    :param input_dict: Dictionnaire à mettre à plat
    :param all_keys: (Facultatif) L'ensemble des clés au pluriel et leur équivalent au singulier pour la transformation
    :param long_keys: Utilise des clés longues (avec l'historique de la hiérarchie)
    :param separator: Séparateur entre les sections et les clés
    :param ignore: Préfixe indiquant que la transformation de cette clé doit être ignorée
    :param auto_id: Suffixe des identifiants uniques ajouté à chaque section
    :param prefix: Préfixe des clés (utile pendant la récursion)
    :return: (Générateur) Combinaisons du dictionnaire
    """
    result = {}
    nested = {}
    dicts = []
    all_keys = all_keys or {}

    # Ajout des identifiants uniques
    if auto_id and prefix is not None and ((auto_id not in input_dict) or not input_dict[auto_id]):
        input_dict[auto_id] = short_identifier()

    # Récupère les clés mises à plat
    for key, value in input_dict.items():
        current_key = all_keys.get(key, key)
        result_key = (prefix + separator + current_key).lstrip(separator)
        if ignore and key.startswith(ignore):
            result_key = key[1:]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # Les dictionnaire imbriqués dans des listes sont à traiter récursivement
            nested_key = result_key if long_keys else current_key
            nested_key = nested_key.rstrip('s') if current_key == key else nested_key
            nested[nested_key] = value
            continue
        elif isinstance(value, dict):
            # Les dictionnaires imbriqués dans des dictionnaires sont récupérés immédiatement par récursivité
            for result in recursive_dict_product(value, all_keys, long_keys, separator, ignore, auto_id, result_key):
                dicts.append(result)
            continue
        result[result_key] = value

    # Retourne le résultat s'il n'y a pas de clés imbriquées
    if not nested:
        # Ajoute les dictionnaires imbriqués
        for d in dicts:
            result.update(d)
        # Retourne le résultat de l'itératon
        yield result
        return

    # Crée les différentes combinaisons des structures imbriquées
    for nested_combos in product(*nested.values()):
        results = [result]
        for nested_key, nested_value in zip(nested, nested_combos):
            # Fusionne les données imbriquées avec les résultats
            if isinstance(nested_value, dict):
                results = [
                    dict(r, **result)
                    for result in recursive_dict_product(nested_value, all_keys, long_keys, separator, ignore, auto_id, nested_key)
                    for r in results
                ]
        for result in results:
            # Ajoute les dictionnaires imbriqués
            for d in dicts:
                result.update(d)
            # Retourne le résultat de l'itération
            yield result


def get_choices_fields(*included_apps):
    """
    Permet de recuperer les choices fields existant dans les modèles
    :param included_apps: liste des applications sur lesquelles on récupère les choices fields
    :return: tuple contenant les choices fields triés par application
    """
    from django.apps import apps
    resultats = dict()
    choices_fields = list()
    included_apps = included_apps or [app.label for app in apps.get_app_configs()]

    for model in apps.get_models():
        app_label = model._meta.app_label
        if app_label in included_apps:
            for field in model._meta.fields:
                if field.choices and field.choices not in choices_fields:
                    choices_fields.append(field.choices)
                    choice_value = ' '.join([app_label, model._meta.model_name, field.name])
                    choice_libelle = '{} ({})'.format(field.verbose_name, model._meta.verbose_name)
                    if app_label in resultats:
                        resultats[app_label].append((choice_value, choice_libelle, ))
                    else:
                        resultats[app_label] = [(choice_value, choice_libelle, )]

    def ordered_choices(resultat):
        for valeur, libelle in sorted(resultat, key=lambda x: x[1]):
            yield valeur, libelle

    def choices_by_application():
        for app_label, choices in sorted(resultats.items()):
            yield str(apps.get_app_config(app_label).verbose_name), tuple(ordered_choices(choices))

    return tuple(choices_by_application())


def get_prefetchs(parent, depth=1, foreign_keys=False, one_to_one=True, one_to_many=False,
                  excludes=None, _model=None, _prefetch='', _level=1):
    """
    Permet de récupérer récursivement tous les prefetch related d'un modèle
    :param parent: Modèle parent
    :param depth: Profondeur de récupération
    :param foreign_keys: Récupère les relations de type foreign-key ?
    :param one_to_one: Récupère les relations de type one-to-one ?
    :param one_to_many: Récupère les relations de type one-to-many ? (peut-être très coûteux selon les données)
    :param excludes: Champs ou types à exclure
    :param _model: Modèle courant (pour la récursivité, nul par défaut)
    :param _prefetch: Nom du prefetch courant (pour la récursivité, vide par défaut)
    :param _level: Profondeur actuelle (pour la récursivité, 1 par défaut)
    :return: Liste des prefetch related associés
    """
    excludes = excludes or []
    results = []
    if _level > depth:
        return results
    model = _model or parent
    for field in model._meta.related_objects:
        if field.name in excludes or (field.related_model in excludes):
            continue
        if field.auto_created and ((field.one_to_one and one_to_one) or (field.one_to_many and one_to_many)):
            accessor_name = field.get_accessor_name()
            recursive_prefetch = accessor_name if model == parent else '__'.join((_prefetch, accessor_name))
            prefetchs = None
            if model == parent or _level < depth:
                prefetchs = get_prefetchs(
                    parent,
                    depth=depth,
                    one_to_one=one_to_one,
                    one_to_many=one_to_many,
                    excludes=excludes,
                    _model=field.related_model,
                    _prefetch=recursive_prefetch,
                    _level=_level + 1)
                results += prefetchs
            if foreign_keys:
                for related in get_related(field.related_model, excludes=excludes, one_to_one=one_to_one, depth=1):
                    results.append('__'.join((recursive_prefetch, related)))
            if not prefetchs:
                results.append(recursive_prefetch)
    return results


def get_related(model, dest=None, excludes=None, one_to_one=False, null=False, depth=1,
                _related='', _models=None, _level=0):
    """
    Permet de récupérer récursivement toutes les relations directes d'un modèle
    :param model: Modèle d'origine
    :param dest: Modèle de destination (facultatif)
    :param excludes: Champs ou types à exclure
    :param one_to_one: Récupère les relations de type one-to-one ?
    :param null: Remonter par les clés étrangères nulles ?
    :param depth: Profondeur de récupération
    :param _related: Nom du chemin de relation courant (pour la récursivité, vide par défaut)
    :param _models: Liste des modèles traversés (pour la récursivité, vide par défaut)
    :param _level: Profondeur actuelle (pour la récursivité, 0 par défaut)
    :return: Liste des relations directes associées
    """
    excludes = excludes or []
    results = []
    if (not dest and _level > depth) or (_models and model in _models):
        return results
    models = (_models or []) + [model]
    if _related and dest == model or (dest is None and _related):
        results.append(_related)
    for field in model._meta.fields:
        if not isinstance(field, (ForeignKey, OneToOneField)) or field.name in excludes \
                or (field.remote_field and field.remote_field.model in excludes) or (not null and field.null):
            continue
        related_path = '__'.join((_related, field.name)) if _related else field.name
        results += get_related(field.remote_field.model, dest=dest, excludes=excludes, depth=depth, null=null,
                               _related=related_path, _models=models, _level=_level + 1)
    if one_to_one:
        for field in model._meta.related_objects:
            if field.one_to_one:
                field_name = field.get_accessor_name()
                if field_name in excludes:
                    continue
                related_path = '__'.join((_related, field_name)) if _related else field_name
                results += get_related(field.related_model, dest=dest, excludes=excludes, depth=depth, null=null,
                                       _related=related_path, _models=models, _level=_level + 1)
    return results


def prefetch_generics(weak_queryset):
    """
    Permet un prefetch des GenericForeignKey
    :param weak_queryset: QuerySet d'origine
    :return: QuerySet avec prefetch
    """
    weak_queryset = weak_queryset.select_related()

    gfks = {}
    for name, gfk in weak_queryset.model.__dict__.items():
        if not isinstance(gfk, GenericForeignKey):
            continue
        gfks[name] = gfk

    data = {}
    for weak_model in weak_queryset:
        for gfk_name, gfk_field in gfks.items():
            related_content_type_id = getattr(
                weak_model, gfk_field.model._meta.get_field(
                    gfk_field.ct_field).get_attname())
            if not related_content_type_id:
                continue
            related_content_type = ContentType.objects.get_for_id(related_content_type_id)
            related_object_id = int(getattr(weak_model, gfk_field.fk_field))

            if related_content_type not in data.keys():
                data[related_content_type] = []
            data[related_content_type].append(related_object_id)

    for content_type, object_ids in data.items():
        model_class = content_type.model_class()
        models = prefetch_generics(model_class.objects.filter(pk__in=object_ids))
        for model in models:
            for weak_model in weak_queryset:
                for gfk_name, gfk_field in gfks.items():
                    related_content_type_id = getattr(
                        weak_model, gfk_field.model._meta.get_field(
                            gfk_field.ct_field).get_attname())
                    if not related_content_type_id:
                        continue
                    related_content_type = ContentType.objects.get_for_id(related_content_type_id)
                    related_object_id = int(getattr(weak_model, gfk_field.fk_field))

                    if related_object_id != model.pk:
                        continue
                    if related_content_type != content_type:
                        continue
                    setattr(weak_model, gfk_name, model)
    return weak_queryset


# Valeurs considérées comme vraies ou fausses
TRUE_VALUES = ['true', 'yes', 'y', '1', _('vrai'), _('oui'), _('o')]
FALSE_VALUES = ['false', 'no', 'n', '0', _('faux'), _('non'), _('n')]


def str_to_bool(value):
    """
    Permet de renvoyer le booleen correspondant à la valeur entrée en paramètre
    :param value: valeur à analyser
    :return: le booleen correspondant ou None si aucune correspondance
    """
    if value is True or value is False:
        return value
    if value is None or str(value).lower() not in TRUE_VALUES + FALSE_VALUES:
        return None
    return str(value).lower() in TRUE_VALUES


def decimal(value=None, precision=None, rounding=ROUND_HALF_EVEN, context=None):
    """
    Permet de gérer la précision et l'arrondi des nombres décimaux
    :param value: Valeur
    :param precision: Précision
    :param rounding: Méthode d'arrondi
    :return: Nombre décimal
    """
    if value is None:
        return Decimal()
    _value = value

    if isinstance(value, str):
        _value = Decimal(value, context=context)
    elif isinstance(value, (int, float)):
        _value = Decimal(str(value), context=context)
    if precision is None:
        return _value

    if isinstance(precision, int):
        precision = Decimal('0.' + '0' * (precision - 1) + '1')
    try:
        return Decimal(_value.quantize(precision, rounding=rounding), context=context)
    except InvalidOperation:
        return _value


def decimal_to_str(value):
    """
    Reformate un nombre décimal en chaîne de caractères
    :param value: Valeur
    """
    return '' if value is None else value if isinstance(value, str) else format(value, 'f').rstrip('0').rstrip('.')


# Regex permettant d'extraire les paramètres d'une URL
REGEX_URL_PARAMS = re.compile(r'\(\?P<([\w_]+)>[^\)]+\)')


def recursive_get_urls(module=None, namespaces=None, attributes=None, model=None, _namespace=None, _current='/'):
    """
    Récupère les URLs d'un module
    :param module: Module à explorer
    :param namespaces: Liste des namespaces à récupérer
    :param attributes: Liste des propriétés à vérifier dans le module
    :param model: Modèle dont on souhaite retrouver les URLs
    :param _namespace: Namespace courant pour la récursion
    :param _current: Fragment d'URL courante pour la récursion
    :return: Générateur
    """
    patterns = []
    namespaces = namespaces or []
    attributes = attributes or ['urlpatterns', 'api_urlpatterns']

    try:
        if not module:
            module = import_module(settings.ROOT_URLCONF)
        patterns = module

        patterns = list(chain(*(getattr(module, attribute, []) for attribute in attributes))) or patterns
        if patterns and isinstance(patterns[-1], str):
            patterns, app_name = patterns
    except (TypeError, ValueError):
        patterns = []

    for pattern in patterns:
        try:
            namespace = _namespace or getattr(pattern, 'namespace', None)
            if namespaces and namespace not in namespaces:
                continue
            url = (_current + pattern.regex.pattern.strip('^$').replace('\\', ''))
            url = re.sub(REGEX_URL_PARAMS, r':\1:', url).replace('?', '')
            url = url.replace('(.+)', ':id:')
            if hasattr(pattern, 'name'):
                key = '{}:{}'.format(namespace, pattern.name) if namespace else pattern.name
                current_model = getattr(pattern.callback.cls, 'model', None)
                if not model or model is current_model:
                    yield key, url
            elif hasattr(pattern, 'namespace') and pattern.urlconf_module:
                yield from recursive_get_urls(
                    pattern.urlconf_module, namespaces=namespaces, attributes=attributes, model=model,
                    _namespace=_namespace or pattern.namespace, _current=url)
        except AttributeError:
            continue


class CustomDict(collections.MutableMapping):
    """
    Surcouche du dictionnaire pour transformer les clés en entrée/sortie
    """

    def __init__(self, *args, **kwargs):
        self._dict = dict()
        self.update(dict(*args, **kwargs))

    def __getitem__(self, key):
        return self._dict[self._transform(key)]

    def __setitem__(self, key, value):
        self._dict[self._transform(key)] = value

    def __delitem__(self, key):
        del self._dict[self._transform(key)]

    def __iter__(self):
        return iter(self._dict)

    def __len__(self):
        return len(self._dict)

    def __repr__(self):
        return repr(self._dict)

    def __str__(self):
        return str(self._dict)

    def __getattr__(self, item):
        try:
            return self.__getattribute__(item)
        except AttributeError:
            return self[item]

    @abc.abstractmethod
    def _transform(self, key):
        return key


class idict(CustomDict):
    """
    Dictionnaire qui transforme les clés en chaînes de caractères
    """

    def _transform(self, key):
        if isinstance(key, (list, tuple)):
            return tuple(self._transform(k) for k in key)
        if isinstance(key, Decimal):
            return decimal_to_str(key)
        return str(key)


def sort_dict(idict):
    """
    Tri l'ensemble des valeurs d'un dictionnaire par les clés
    :param idict: Dictionnaire
    :return: Dictionnaire trié
    """
    return json_decode(json_encode(idict, sort_keys=True), object_pairs_hook=collections.OrderedDict)


class Null(object):
    """
    Objet nul absolu
    """
    _instances = {}

    def __new__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Null, cls).__new__(cls, *args, **kwargs)
        return cls._instances[cls]

    def __init__(self, *args, **kwargs):
        pass

    def __repr__(self):
        return "null"

    def __str__(self):
        return ""

    def __eq__(self, other):
        return id(self) == id(other) or other is None

    def __iter__(self):
        return iter([])

    def __len__(self):
        return 0

    # Null est faux dans un contexte booléen
    __nonzero__ = __bool__ = lambda self: False

    # Null se retourne lui-même en toutes circonstances
    nullify = lambda self, *args, **kwargs: self

    __call__ = nullify
    __getattr__ = __setattr__ = __delattr__ = nullify
    __cmp__ = __ne__ = __lt__ = __gt__ = __le__ = __ge__ = nullify
    __pos__ = __neg__ = __abs__ = __invert__ = nullify
    __add__ = __sub__ = __mul__ = __mod__ = __pow__ = nullify
    __floordiv__ = __div__ = __truediv__ = __divmod__ = nullify
    __lshift__ = __rshift__ = __and__ = __or__ = __xor__ = nullify
    __radd__ = __rsub__ = __rmul__ = __rmod__ = __rpow__ = nullify
    __rfloordiv__ = __rdiv__ = __rtruediv__ = __rdivmod__ = nullify
    __rlshift__ = __rrshift__ = __rand__ = __ror__ = __rxor__ = nullify
    __iadd__ = __isub__ = __imul__ = __imod__ = __ipow__ = nullify
    __ifloordiv__ = __idiv__ = __itruediv__ = __idivmod__ = nullify
    __ilshift__ = __irshift__ = __iand__ = __ior__ = __ixor__ = nullify
    __getitem__ = __setitem__ = __delitem__ = nullify
    __getslice__ = __setslice__ = __delslice__ = nullify
    __reversed__ = nullify
    __contains__ = __missing__ = nullify
    __enter__ = __exit__ = nullify


# Valeur nulle absolue
null = Null()


def to_object(contexte, name='Context', default=None):
    """
    Transforme un dictionnaire en objet ou une liste de dictionnaire en liste d'objets
    :param contexte: Dictionnaire ou liste de dictionnaires
    :param name: Nom de l'objet
    :param default: Valeur par défaut des attributs
    :return: Objet ou liste d'objets
    """
    def _getattr(s, k):
        try:
            object.__getattribute__(s, k)
        except AttributeError:
            return default

    if isinstance(contexte, list):
        return [to_object(ctx, name) for ctx in contexte]
    elif isinstance(contexte, dict):
        attrs = dict(
            __str__=lambda s: str(contexte),
            __repr__=lambda s: repr(contexte),
            __getitem__=lambda s, k: getattr(s, k, default),
            __getattr__=lambda s, k: _getattr(s, k))
        for key, value in contexte.items():
            if isinstance(value, (list, dict)):
                attrs[key] = to_object(value, name)
                continue
            attrs[key] = value
        return type(name, (object, ), attrs)()
    return contexte


def file_is_text(file):
    """
    Vérifie qu'un fichier est au format texte et non binaire
    :param file: Chemin vers le fichier
    :return: Vrai si le fichier est au format texte, faux s'il est au format binaire
    """
    textchars = bytearray([7, 8, 9, 10, 12, 13, 27]) + bytearray(range(0x20, 0x100))
    is_plaintext = lambda _bytes: not bool(_bytes.translate(None, textchars))
    with open(file, 'rb') as f:
        return is_plaintext(f.read(1024))


def base64_encode(data):
    """
    Encode une chaîne en base64
    :param data: Chaîne à encoder
    :return: Chaîne encodée en base64
    """
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes
    return urlsafe_base64_encode(force_bytes(data)).decode()


def base64_decode(data):
    """
    Décode une chaîne en base64
    :param data: Chaîne base64 à décoder
    :return: Chaîne décodée
    """
    from django.utils.http import urlsafe_base64_decode
    from django.utils.encoding import force_text
    return force_text(urlsafe_base64_decode(data))


def short_identifier():
    """
    Crée un identifiant court et (presque) unique
    """
    alphabet = tuple('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
    base = len(alphabet)
    num = uuid4().time
    digits = []
    while num > 0:
        num, rem = divmod(num, base)
        digits.append(alphabet[rem])
    return ''.join(reversed(digits))


# Encoder spécifique pour les valeurs nulles
class JsonEncoder(JSONEncoder):
    def default(self, obj):
        if obj is null:
            return None
        return super().default(obj)


# JSON (de-)serialization
json_encode = lambda data, **options: json.dumps(data, cls=JsonEncoder, **options)
json_decode = lambda data, **options: json.loads(data, parse_float=decimal, encoding=settings.DEFAULT_CHARSET, **options)
