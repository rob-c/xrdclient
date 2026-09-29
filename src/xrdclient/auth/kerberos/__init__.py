"""A pure-Python Kerberos 5 client: just enough to log in to an xrootd server.

What XRootD's ``krb5`` security plugin wants is one raw AP-REQ for its
service principal (``xrootd/host@REALM``), and - when the server runs with
``-exptkn`` - a KRB-CRED carrying a forwarded ticket-granting ticket. This
package builds both, from what ``kinit`` left in a credential cache:

* :mod:`.model` - principals and credentials, the vocabulary of the rest;
* :mod:`.asn1` - the DER encoding of the RFC 4120 messages involved;
* :mod:`.ccache` - the MIT/Heimdal FILE credential cache, keys included,
  and the marshalled principal and credential every cache type shares;
* :mod:`.kcm` - ``KCM:`` caches, over the KCM daemon's Unix socket;
* :mod:`.keyring` - ``KEYRING:`` caches, in Linux kernel keyrings;
* :mod:`.caches` - opening a cache by name, whichever of those it is;
* :mod:`.profile` - ``krb5.conf``: realms, KDCs, and the domain mapping;
* :mod:`.kdc` - talking to a KDC over UDP and TCP;
* :mod:`.tgs` - the TGS exchange, and the AP-REQ and KRB-CRED themselves.

The cryptography is :mod:`xrdclient.crypto.rfc3961`. It does not do the
initial (AS) exchange - that is ``kinit``'s job, and needs a password or a
keytab this library should never hold.
"""
