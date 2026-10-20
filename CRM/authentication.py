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

        user = self.get_user_for_phone_number_id(phone_number_id)
        if not user:
            raise AuthenticationFailed(f"No account or organization found matching phone_number_id '{phone_number_id}'")

        # Attach phone_number_id directly to request for easy access in views
        request.phone_number_id = phone_number_id
        return (user, phone_number_id)

    def get_user_for_phone_number_id(self, phone_number_id):
        # 1. Check in ClientAccount
        client = ClientAccount.objects.select_related('tech_provider__owner').filter(phone_number_id=phone_number_id).first()
        if client:
            # Check for owner/member user linked to this client
            member = ClientMember.objects.select_related('user').filter(client=client).order_by('id').first()
            if member and member.user:
                return member.user
            # Fallback to tech_provider owner
            if client.tech_provider and client.tech_provider.owner:
                return client.tech_provider.owner

        # 2. Check in WABAAccount (Tech Provider / Organization level)
        waba = WABAAccount.objects.select_related('organization__owner').filter(phone_number_id=phone_number_id).first()
        if waba and waba.organization and waba.organization.owner:
            return waba.organization.owner

        # If no account matched, return None so AuthenticationFailed is raised
        return None

    def authenticate_header(self, request):
        return self.keyword
