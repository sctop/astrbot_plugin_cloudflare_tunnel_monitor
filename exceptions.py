# CLIENT/USER SIDE 用户端
class TunnelAlreadyAddedException(Exception):
    pass


# CLIENT/USER SIDE 用户端
class TunnelAlreadyRemovedException(Exception):
    def __str__(self):
        return "TunnelAlreadyRemovedException"


# CLIENT/USER SIDE 用户端
class TunnelMultipleAliasesException(Exception):
    """多个tunnels共用一个别名"""
    def __str__(self):
        return "TunnelMultipleAliasesException"

# CLIENT/USER SIDE 用户端
class TunnelNotFoundException(Exception):
    def __str__(self):
        return "TunnelNotFoundException"

# CLIENT/USER SIDE 用户端
class CloudflareAPI429Exception(Exception):
    def __str__(self):
        return "CloudflareAPI429Exception"

# CLIENT/USER SIDE 用户端
class CloudflareAPIRequestError(Exception):
    def __str__(self):
        return "CloudflareAPIRequestError"


# SERVER/REMOTE SIDE 远程端
class TunnelRemotelyRemovedException(Exception):
    def __str__(self):
        return "TunnelRemotelyRemovedException"
