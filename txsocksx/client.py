# Copyright (c) Aaron Gallagher <_@habnab.it>
# See COPYING for details.

import struct

from parsley import makeProtocol, stack
from twisted.internet import protocol, defer, interfaces
from twisted.python import failure
from zope.interface import implements

import txsocksx.constants as c, txsocksx.errors as e
from txsocksx import grammar

def socks_host(host):
    return chr(c.ATYP_DOMAINNAME) + chr(len(host)) + host

class SOCKS5ClientTransport(object):
    def __init__(self, wrappedClient):
        self.wrappedClient = wrappedClient
        self.transport = self.wrappedClient.transport

    def __getattr__(self, attr):
        return getattr(self.transport, attr)

class SOCKS5Sender(object):
    def __init__(self, transport):
        self.transport = transport

    def sendAuthMethods(self, methods):
        self.transport.write(
            struct.pack('!BB', c.VER_SOCKS5, len(methods)) + ''.join(methods))

    def sendLogin(self, username, password):
        self.transport.write(
            '\x01'
            + chr(len(username)) + username
            + chr(len(password)) + password)

    def sendRequest(self, command, host, port):
        data = struct.pack('!BBB', c.VER_SOCKS5, command, c.RSV)
        port = struct.pack('!H', port)
        self.transport.write(data + socks_host(host) + port)


class SOCKS5AuthDispatcher(object):
    def __init__(self, wrapped):
        self.w = wrapped

    def __getattr__(self, attr):
        return getattr(self.w, attr)

    def authSelected(self, method):
        if method not in self.w.factory.methods:
            raise e.MethodsNotAcceptedError('no method proprosed was accepted',
                                            self.w.factory.methods, method)
        authMethod = getattr(self.w, 'auth_' + self.w.authMethodMap[method])
        authMethod(*self.w.factory.methods[method])


class SOCKS5Receiver(object):
    implements(interfaces.ITransport)
    otherProtocol = None
    currentRule = 'SOCKS5ClientState_initial'

    def __init__(self, sender):
        self.sender = sender

    def prepareParsing(self, parser):
        self.factory = parser.factory
        self.sender.sendAuthMethods(self.factory.methods)

    authMethodMap = {
        c.AUTH_ANONYMOUS: 'anonymous',
        c.AUTH_LOGIN: 'login',
    }

    def auth_anonymous(self):
        self._sendRequest()

    def auth_login(self, username, password):
        self.sender.sendLogin(username, password)
        self.currentRule = 'SOCKS5ClientState_readLoginResponse'

    def loginResponse(self, success):
        if not success:
            raise e.LoginAuthenticationFailed(
                'username/password combination was rejected')
        self._sendRequest()

    def _sendRequest(self):
        self.sender.sendRequest(
            c.CMD_CONNECT, self.factory.host, self.factory.port)
        self.currentRule = 'SOCKS5ClientState_readResponse'

    def serverResponse(self, status, address, port):
        if status != c.SOCKS5_GRANTED:
            raise e.ConnectionError('connection rejected by SOCKS server',
                                    status,
                                    e.socks5ErrorMap.get(status, status))
        self.factory.proxyConnectionEstablished(self)
        self.currentRule = 'SOCKSState_readData'

    def proxyEstablished(self, other):
        self.otherProtocol = other
        other.makeConnection(SOCKS5ClientTransport(self.sender))

    def dataReceived(self, data):
        self.otherProtocol.dataReceived(data)

    def finishParsing(self, reason):
        if self.otherProtocol:
            self.otherProtocol.connectionLost(reason)
        else:
            self.factory.proxyConnectionFailed(reason)

SOCKS5Client = makeProtocol(
    grammar.grammarSource,
    SOCKS5Sender,
    stack(SOCKS5AuthDispatcher, SOCKS5Receiver),
    grammar.bindings)

class SOCKS5ClientFactory(protocol.ClientFactory):
    protocol = SOCKS5Client

    authMethodMap = {
        'anonymous': c.AUTH_ANONYMOUS,
        'login': c.AUTH_LOGIN,
    }

    def __init__(self, host, port, proxiedFactory, methods={'anonymous': ()}):
        if not methods:
            raise ValueError('no auth methods were specified')
        self.host = host
        self.port = port
        self.proxiedFactory = proxiedFactory
        self.methods = dict(
            (self.authMethodMap[method], value)
            for method, value in methods.iteritems())
        self.deferred = defer.Deferred()

    def proxyConnectionFailed(self, reason):
        self.deferred.errback(reason)

    def clientConnectionFailed(self, connector, reason):
        self.proxyConnectionFailed(reason)

    def proxyConnectionEstablished(self, proxyProtocol):
        proto = self.proxiedFactory.buildProtocol(
            proxyProtocol.sender.transport.getPeer())
        # XXX: handle the case of `proto is None`
        proxyProtocol.proxyEstablished(proto)
        self.deferred.callback(proto)


class SOCKS5ClientEndpoint(object):
    implements(interfaces.IStreamClientEndpoint)

    def __init__(self, host, port, proxyEndpoint, anonymousAuth=True,
                 loginAuth=None):
        self.host = host
        self.port = port
        self.proxyEndpoint = proxyEndpoint
        self.anonymousAuth = anonymousAuth
        self.loginAuth = loginAuth

    def connect(self, fac):
        proxyFac = SOCKS5ClientFactory(
            self.host, self.port, fac, self.anonymousAuth, self.loginAuth)
        self.proxyEndpoint.connect(proxyFac)
        # XXX: maybe use the deferred returned here? need to more different
        # ways/times a connection can fail before connectionMade is called.
        return proxyFac.deferred
