# coding: utf-8
from django.core.exceptions import FieldDoesNotExist
from django.db.models.query import F, EmptyResultSet, Prefetch, QuerySet
from rest_framework import serializers
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError

from common.api.utils import AGGREGATES, CACHE_PREFIX, CACHE_TIMEOUT, RESERVED_QUERY_PARAMS, url_value, parse_filters
from common.api.fields import ChoiceDisplayField, ReadOnlyObjectField
from common.models import Entity, MetaData
from common.settings import settings
from common.utils import get_field_by_path, str_to_bool


class CommonModelViewSet(viewsets.ModelViewSet):
    """
    Définition commune de ModelViewSet pour l'API REST
    """

    def get_serializer_class(self):
        # Le serializer par défaut est utilisé en cas de modification/suppression
        default_serializer = getattr(self, 'default_serializer', None)
        if default_serializer and self.action not in ['list', 'retrieve']:
            return default_serializer

        # Le serializer peut être substitué en fonction des paramètres d'appel de l'API
        url_params = self.request.query_params.dict()
        if default_serializer:
            # Ajoute les champs d'aggregation au serializer
            aggregations = {}
            for aggregate in AGGREGATES.keys():
                for field in url_params.get(aggregate, '').split(','):
                    if not field:
                        continue
                    aggregations[field + '_' + aggregate] = serializers.ReadOnlyField()
            if 'group_by' in url_params or aggregations:
                fields = {
                    field: serializers.ReadOnlyField()
                    for field in url_params.get('group_by', '').split(',')}
                fields.update(aggregations)
                # Un serializer avec les données groupées est créé à la volée
                return type(default_serializer.__name__, (serializers.Serializer, ), fields)
            elif 'fields' in url_params:
                fields = {}
                for field in url_params.get('fields').split(','):
                    # Champ spécifique en cas d'énumération
                    choices = get_field_by_path(self.queryset.model, field).choices
                    if choices and str_to_bool(url_params.get('display')):
                        fields[field + '_display'] = ChoiceDisplayField(choices=choices, source=field.replace('__', '.'))
                    # Champ spécifique pour l'affichage de la valeur
                    fields[field] = ReadOnlyObjectField(source=field.replace('__', '.') if '__' in field else None)
                # Un serializer avec restriction des champs est créé à la volée
                return type(default_serializer.__name__, (serializers.Serializer, ), fields)
            elif str_to_bool(url_params.get('simple')):
                return getattr(self, 'simple_serializer', default_serializer)
        return super().get_serializer_class()

    def perform_create(self, serializer):
        if issubclass(serializer.Meta.model, Entity):
            return serializer.save(_current_user=self.request.user)
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        if issubclass(serializer.Meta.model, Entity):
            return serializer.save(_current_user=self.request.user)
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        if isinstance(instance, Entity):
            return instance.delete(_current_user=self.request.user)
        return super().perform_destroy(instance)

    def list(self, request, *args, **kwargs):
        # Détournement en cas d'aggregation sans annotation ou de non QuerySet
        queryset = self.get_queryset()
        if not isinstance(queryset, QuerySet):
            from rest_framework.response import Response
            return Response(queryset)
        try:
            return super().list(request, *args, **kwargs)
        except (AttributeError, FieldDoesNotExist) as e:
            self.queryset_error = e
            raise ValidationError("fields: {}".format(e))

    def paginate_queryset(self, queryset):
        # Aucune pagination si toutes les données sont demandées ou qu'il ne s'agit pas d'un QuerySet
        if not isinstance(queryset, QuerySet) or str_to_bool(self.request.query_params.get('all', None)):
            return None
        return super().paginate_queryset(queryset)

    def get_queryset(self):
        # Evite la ré-évaluation du QuerySet en cas d'erreur
        if getattr(self, 'queryset_error', False):
            return

        try:
            # Détournement en cas d'aggregation sans annotation ou de non QuerySet
            queryset = super().get_queryset()
            if not isinstance(queryset, QuerySet):
                return queryset

            options = dict(aggregates=None, distinct=None, filters=None, order_by=None)
            url_params = self.request.query_params.dict()

            # Mots-clés réservés dans les URLs
            default_reserved_query_params = ['format'] + ([
                self.paginator.page_query_param,
                self.paginator.page_size_query_param] if self.paginator else [])
            reserved_query_params = default_reserved_query_params + RESERVED_QUERY_PARAMS

            # Critères de recherche dans le cache
            cache_key = url_params.pop('cache', None)
            if cache_key:
                from django.core.cache import cache
                cache_params = cache.get(CACHE_PREFIX + cache_key, {})
                new_url_params = {}
                new_url_params.update(**cache_params)
                new_url_params.update(**url_params)
                url_params = new_url_params
                new_cache_params = {key: value for key, value in url_params.items() if key not in default_reserved_query_params}
                if new_cache_params:
                    from django.utils.timezone import now
                    from datetime import timedelta
                    cache_timeout = int(url_params.pop('timeout', CACHE_TIMEOUT)) or None
                    cache.set(CACHE_PREFIX + cache_key, new_cache_params, timeout=cache_timeout)
                    options['cache_expires'] = now() + timedelta(seconds=cache_timeout)
                cache_url = '{}?cache={}'.format(self.request.build_absolute_uri(self.request.path), cache_key)
                plain_url = cache_url
                for key, value in url_params.items():
                    url_param = '&{}={}'.format(key, value)
                    if key in default_reserved_query_params:
                        cache_url += url_param
                    plain_url += url_param
                options['raw_url'] = plain_url
                options['cache_url'] = cache_url
                options['cache_data'] = new_cache_params

            # Erreurs silencieuses
            silent = str_to_bool(url_params.get('silent', None))

            # Requête simplifiée et/ou extraction de champs spécifiques
            fields = url_params.get('fields', '').replace('.', '__')
            if str_to_bool(url_params.get('simple', None)) or fields:
                # Supprime la récupération des relations
                queryset = queryset.select_related(None).prefetch_related(None)
                # Champs spécifiques
                try:
                    relateds = set()
                    field_names = set()
                    for field in fields.split(','):
                        if not field:
                            continue
                        field_names.add(field)
                        *related, field_name = field.split('__')
                        if related:
                            relateds.add('__'.join(related))
                    if relateds:
                        queryset = queryset.select_related(*relateds)
                    if field_names:
                        queryset = queryset.only(*field_names)
                except Exception as error:
                    if not silent:
                        raise ValidationError("fields: {}".format(error))
            else:
                # Récupération des métadonnées
                metadatas = str_to_bool(url_params.get('meta', False))
                if metadatas and hasattr(self, 'metadatas'):
                    # Permet d'éviter les conflits entre prefetch lookups identiques
                    viewset_lookups = [
                        prefetch if isinstance(prefetch, str) else prefetch.prefetch_through
                        for prefetch in queryset._prefetch_related_lookups]
                    lookups_metadatas = []
                    for lookup in self.metadatas or []:
                        if isinstance(lookup, str):
                            lookup = Prefetch(lookup)
                        if lookup.prefetch_through not in viewset_lookups:
                            lookups_metadatas.append(lookup)
                        lookup.queryset = MetaData.objects.select_valid()
                    if lookups_metadatas:
                        queryset = queryset.prefetch_related(*lookups_metadatas)

            # Filtres (dans une fonction pour être appelé par les aggregations sans group_by)
            def do_filter(queryset):
                try:
                    filters = {}
                    excludes = {}
                    for key, value in url_params.items():
                        if value.startswith('[') and value.endswith(']'):
                            value = F(value[1:-1])
                        if key not in reserved_query_params:
                            key = key[1:] if key.startswith('@') else key
                            if key.startswith('-'):
                                excludes[key[1:]] = url_value(key[1:], value)
                            else:
                                filters[key] = url_value(key, value)
                    if filters:
                        queryset = queryset.filter(**filters)
                    if excludes:
                        queryset = queryset.exclude(**excludes)
                    # Filtres génériques
                    others = url_params.get('filters', None)
                    if others:
                        queryset = queryset.filter(parse_filters(others))
                    if filters or excludes or others:
                        options['filters'] = True
                except Exception as error:
                    if not silent:
                        raise ValidationError("filters: {}".format(error))
                    options['filters'] = False
                    if settings.DEBUG:
                        options['filters_error'] = str(error)
                return queryset

            # Aggregations
            try:
                aggregations = {}
                for aggegate, function in AGGREGATES.items():
                    for field in url_params.get(aggegate, '').split(','):
                        if not field:
                            continue
                        aggregations[field + '_' + aggegate] = function(field)
                group_by = url_params.get('group_by', None)
                if group_by:
                    _queryset = queryset.values(*group_by.split(','))
                    if aggregations:
                        _queryset = _queryset.annotate(**aggregations)
                    else:
                        _queryset = _queryset.distinct()
                    queryset = _queryset
                    options['aggregates'] = True
                elif aggregations:
                    queryset = do_filter(queryset)  # Filtres éventuels
                    return queryset.aggregate(**aggregations)
            except Exception as error:
                if not silent:
                    raise ValidationError("aggregates: {}".format(error))
                options['aggregates'] = False
                if settings.DEBUG:
                    options['aggregates_error'] = str(error)

            # Filtres
            queryset = do_filter(queryset)

            # Tris
            try:
                order_by = url_params.get('order_by', None)
                if order_by:
                    _queryset = queryset.order_by(*order_by.split(','))
                    str(_queryset.query)  # Force SQL evaluation to retrieve exception
                    queryset = _queryset
                    options['order_by'] = True
            except EmptyResultSet:
                pass
            except Exception as error:
                if not silent:
                    raise ValidationError("order_by: {}".format(error))
                options['order_by'] = False
                if settings.DEBUG:
                    options['order_by_error'] = str(error)

            # Distinct
            try:
                distinct = url_params.get('distinct', None)
                if distinct:
                    distincts = distinct.split(',')
                    if str_to_bool(distinct) is not None:
                        distincts = []
                    queryset = queryset.distinct(*distincts)
                    options['distinct'] = True
            except EmptyResultSet:
                pass
            except Exception as error:
                if not silent:
                    raise ValidationError("distinct: {}".format(error))
                options['distinct'] = False
                if settings.DEBUG:
                    options['distinct_error'] = str(error)

            # Ajout des options de filtres/tris dans la pagination
            if self.paginator and hasattr(self.paginator, 'additional_data'):
                # Force un tri sur la clé primaire en cas de pagination
                if hasattr(queryset, 'ordered') and not queryset.ordered:
                    queryset = queryset.order_by(*(getattr(queryset, '_fields', None) or [queryset.model._meta.pk.name]))
                self.paginator.additional_data = dict(options=options)
            return queryset
        except ValidationError as e:
            self.queryset_error = e
            raise e


class UserViewSet(CommonModelViewSet):
    """
    ViewSet spécifique pour l'utilisateur
    """

    def check_permissions(self, request):
        # Autorise l'utilisateur à modifier ses propres informations ou les informations des utilisateurs en dessous
        current_user = request.user
        if current_user.is_superuser:
            return True
        elif self.action in ['create']:
            # Autorise la création pour tout le monde
            return True
        elif self.action in ['update', 'partial_update']:
            # Autorise la modification de soi-même ou d'un autre utilisateur de rang inférieur
            user = self.get_object()
            if (current_user == user) or (current_user.is_staff and not (user.is_staff or user.is_superuser)):
                return True
        # Applique le système de permissions dans les autres cas
        return super().check_permissions(request)

    def check_data(self, data):
        # Assure que l'utilisateur ne s'octroie pas des droits qu'il ne peut pas avoir
        user = self.request.user
        if not user:
            if not user.is_staff and not user.is_superuser:
                data['is_active'] = True
            if not user.is_superuser:
                data['is_staff'] = False
            if not user.is_superuser:
                data['is_superuser'] = False
        if 'groups' in data and data.get('groups'):
            if not user:
                data['groups'] = []
            elif not user.is_superuser:
                groups = user.groups.all()
                data['groups'] = list(set(groups) & set(data.get('groups')))
        if 'user_permissions' in data and data.get('user_permissions'):
            if not user:
                data['user_permissions'] = []
            elif not user.is_superuser:
                user_permissions = user.user_permissions.all()
                data['user_permissions'] = list(set(user_permissions) & set(data.get('user_permissions')))

    def perform_create(self, serializer):
        self.check_data(serializer.validated_data)
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self.check_data(serializer.validated_data)
        super().perform_update(serializer)
