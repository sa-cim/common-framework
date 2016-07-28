# coding: utf-8
from common.api.permissions import CurrentUserPermissions
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from rest_framework.authtoken.models import Token

from common.api.serializers import UserSerializer
from common.api.utils import create_api
from common.api.viewsets import UserViewSet
from common.models import MODELS, GroupMetaData, UserMetaData


# Modèle utilisateur courant
User = get_user_model()

# Serializers et viewsets crées par défaut
SERIALIZERS = {}
VIEWSETS = {}

# Héritages des serializers et viewsets
SERIALIZERS_BASE = {
    User: (UserSerializer, ),
}
VIEWSETS_BASE = {
    User: (UserViewSet, ),
}

# Données complémentaires à ajouter aux serializers et viewsets
SERIALIZERS_DATA = {}
VIEWSETS_DATA = {}

# Surcharges du queryset du viewset principal
QUERYSETS = {}

# Métadonnees des serializers
METADATAS = {}

# Configuration des serializers
CONFIGS = {
    Group: dict(depth=1, permissions=[CurrentUserPermissions]),
    GroupMetaData: dict(depth=1, permissions=[CurrentUserPermissions]),
    User: dict(many_to_many=True, depth=1, permissions=[CurrentUserPermissions]),
    UserMetaData: dict(many_to_many=True, depth=1, permissions=[CurrentUserPermissions]),
}

# Configuration par défaut
DEFAULT_CONFIG = dict(depth=1)

# Précise les filtres à appliquer sur les permissions spécifiques par utilisateur
CurrentUserPermissions.filters.update({
    User: lambda request: dict(id=request.user.id),
    Group: lambda request: dict(user=request.user),
    UserMetaData: lambda request: dict(user=request.user),
    GroupMetaData: lambda request: dict(group__user=request.user),
})

# Création des APIs REST standard pour les modèles de cette application
router, *_ = create_api(User, Group, Permission, ContentType, LogEntry, Token, *MODELS)
