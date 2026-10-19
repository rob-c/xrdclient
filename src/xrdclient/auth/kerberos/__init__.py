"""The pure-Python Kerberos client's modules, by the names xrdclient published them under.

The client itself - credential caches (``FILE:``, ``DIR:``, ``KCM:``,
``KEYRING:``), ``krb5.conf``, the KDC transport, the TGS exchange and the
AP-REQ and KRB-CRED messages - moved to :mod:`xrdsec.krb5` in 0.4.0, where
xgfalclient shares it. Each module here is that library's module under its
pre-0.4.0 name (``xrdclient.auth.kerberos.ccache is xrdsec.krb5.ccache``),
except :mod:`.caches`, which also keeps xrdclient's own ``$XRD_KRB5_BACKEND``
choice. What is XRootD's - the ``krb5\\0`` credential framing, the service
principal taken from the server's offer, ``-exptkn`` forwarding - is
:mod:`xrdclient.auth.krb5`.

New code imports :mod:`xrdsec.krb5`.
"""
