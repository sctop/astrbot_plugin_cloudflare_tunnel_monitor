# CLIENT/USER SIDE 用户端
class TunnelAlreadyAddedException(Exception):
    pass


# CLIENT/USER SIDE 用户端
class TunnelAlreadyRemovedException(Exception):
    pass


# CLIENT/USER SIDE 用户端
class TunnelMultipleAliasesException(Exception):
    """多个tunnels共用一个别名"""
    pass

# CLIENT/USER SIDE 用户端
class TunnelNotFoundException(Exception):
    pass

# CLIENT/USER SIDE 用户端
class CloudFlareAPI429Exception(Exception):
    pass

# CLIENT/USER SIDE 用户端
class CloudFlareAPIRequestError(Exception):
    pass


# SERVER/REMOTE SIDE 远程端
class TunnelRemotelyRemovedException(Exception):
    pass
