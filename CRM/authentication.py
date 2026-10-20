import logging
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import get_user_model
from CRM.models import ClientAccount, ClientMember, WABAAccount

logger = logging.getLogger(__name__)
User = get_user_model()


class PhoneNumberIdAuthentication(BaseAuthentication):
    """
    Custom DRF Authentication allowing access using:
        Authorization: PhoneId <phone_number_id>
    or fallback query param / header:
        X-Phone-Number-Id: <phone_number_id>
        ?phone_number_id=<phone_number_id>
    """
    keyword = 'PhoneId'

    def authenticate(self, request):
        auth = get_authorization_header(request).split()

        phone_number_id = None

        if auth and len(auth) >= 2 and auth[0].decode('utf-8').lower() == self.keyword.lower():
            phone_number_id = auth[1].decode('utf-8').strip()
        elif 'HTTP_X_PHONE_NUMBER_ID' in request.META:
            phone_number_id = request.META['HTTP_X_PHONE_NUMBER_ID'].strip()
        elif 'phone_number_id' in request.query_params:
            phone_number_id = request.query_params.get('phone_number_id', '').strip()

        if not phone_number_id:
            return None  # Let next authentication class (e.g. JWTAuthentication) handle it

        # 1. Search in WABAAccount (Organization / Tech Provider level)
        waba = WABAAccount.objects.select_related('organization__owner').filter(phone_number_id=phone_number_id).first()
        if waba and waba.organization and waba.organization.owner:
            user = waba.organization.owner
            request.organization = waba.organization
            request.waba_account = waba
            request.client_account = None
            request.phone_number_id = phone_number_id
            return (user, phone_number_id)

        # 2. Search in ClientAccount (Client level)
        client = ClientAccount.objects.select_related('tech_provider__owner').filter(phone_number_id=phone_number_id).first()
        if client:
            member = ClientMember.objects.select_related('user').filter(client=client).order_by('id').first()
            user = member.user if (member and member.user) else (client.tech_provider.owner if client.tech_provider else None)
            if user:
                request.organization = client.tech_provider
                request.client_account = client
                request.waba_account = None
                request.phone_number_id = phone_number_id
                return (user, phone_number_id)

        # If no account matched, raise AuthenticationFailed
        raise AuthenticationFailed(f"No account or organization found matching phone_number_id '{phone_number_id}'")

    def authenticate_header(self, request):
        return self.keyword
