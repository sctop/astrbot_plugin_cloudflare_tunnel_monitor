import asyncio
import copy
import json
import os
import datetime
import time
from typing import Dict, List, Literal, Optional, Callable, Awaitable
from uuid import UUID
from zoneinfo import ZoneInfo
from collections import OrderedDict

import pydantic
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from cloudflare import Cloudflare, DefaultHttpxClient, RateLimitError, APIError
from cloudflare.types.shared.cloudflare_tunnel import CloudflareTunnel

from .exceptions import TunnelAlreadyAddedException, TunnelAlreadyRemovedException, TunnelNotFoundException, \
    CloudFlareAPI429Exception, CloudFlareAPIRequestError
from .utils import TunnelStatusUtils, TimeUtils, FileUtils


class TunnelStatusModel(pydantic.BaseModel):
    id: str  # uuid
    name: str  # user-friendly name

    status: Literal['inactive',  # never run
    'degraded',  # active, but unhealthy (intermittent connection issues)
    'healthy',  # everything fine
    'down']  # no connections
    tun_type: Literal["cfd_tunnel", "warp_connector", "warp", "magic", "ip_sec", "gre", "cni"]

    created_at: datetime.datetime
    conns_active_at: Optional[datetime.datetime]
    conns_inactive_at: Optional[datetime.datetime]

    conns_nums: int
    conns_edge_dc: List[str]  # aggregated results
    replica_nums: int

    def clone(self) -> "TunnelStatusModel":
        return copy.deepcopy(self)

    @classmethod
    def get_default_values(cls, uuid: str) -> "TunnelStatusModel":
        """仅应作为临时措施时调用（如添加新tunnel时）"""
        return cls(id=uuid, name='NoneNoneNoneNone', status='down', tun_type="cfd_tunnel",
                   conns_active_at=datetime.datetime.now(),
                   conns_inactive_at=datetime.datetime.now(),
                   created_at=datetime.datetime.now(),
                   replica_nums=0, conns_nums=0,
                   conns_edge_dc=[])

    @classmethod
    def create_from_tunnel_entry(cls, entry: CloudflareTunnel) -> "TunnelStatusModel":
        return cls(id=entry.id, name=entry.name, status=entry.status if entry.status else 'inactive',
                   tun_type=entry.tun_type if entry.tun_type else 'cfd_tunnel',
                   created_at=entry.created_at, conns_active_at=entry.conns_active_at,
                   conns_inactive_at=entry.conns_inactive_at,
                   conns_nums=len(entry.connections),
                   conns_edge_dc=list(set([j.colo_name for j in entry.connections])),
                   replica_nums=len(list(set([j.client_id for j in entry.connections]))))


class NotificationSender:
    def __init__(self, callback_func: Callable[[str, MessageChain], Awaitable[None]], timezone_name: str) -> None:
        self.send_func = callback_func
        self.timezone_name = timezone_name

    def get_current_time(self) -> str:
        current = datetime.datetime.now(tz=ZoneInfo(self.timezone_name))
        return current.strftime("%Y-%m-%d %H:%M:%S")

    def _append_message_text_for_active_tunnel_info(self, tunnel: TunnelStatusModel,
                                                    lines: List[str]) -> List[str]:
        lines.extend([
            f'🚇 {tunnel.name} {self._get_status_emoji(tunnel.status)}',
            f'      🏷️ID: {tunnel.id}',
            (
                f'      📶连接时间: {TimeUtils.get_datetime_strftime_in_tz(tunnel.conns_active_at, self.timezone_name)} '
                f'({TimeUtils.get_ddhhmmss_from_seconds(time.time() - tunnel.conns_active_at.timestamp())})'
            ),
            f'      📲连接数: {tunnel.conns_nums} ({", ".join(tunnel.conns_edge_dc)})',
            f'      👥Replica 数: {tunnel.replica_nums}',
            f'      🌐当前状态: {self._get_status_string(tunnel.status)}'
        ])
        return lines

    def _append_message_text_for_tunnel_info_list(self, tunnel: TunnelStatusModel) -> List[str]:
        lines = [
            f'🚇 {tunnel.name} {self._get_status_emoji(tunnel.status)}',
            f'      🏷️ID: {tunnel.id}',
            f'      🐣创建时间: {TimeUtils.get_datetime_strftime_in_tz(tunnel.created_at, self.timezone_name)}'
        ]

        if tunnel.status != 'inactive' and tunnel.status != 'down':
            lines.append(
                f'      📶连接时间: {TimeUtils.get_datetime_strftime_in_tz(tunnel.conns_active_at, self.timezone_name)} '
                f'({TimeUtils.get_ddhhmmss_from_seconds(time.time() - tunnel.conns_active_at.timestamp())})'
            )
            lines.append(f'      📲连接数: {tunnel.conns_nums} ({", ".join(tunnel.conns_edge_dc)})')
            lines.append(f'      👥Replica 数: {tunnel.replica_nums}')
        else:
            lines.append(
                f'      📶断开时间: {TimeUtils.get_datetime_strftime_in_tz(tunnel.conns_inactive_at, self.timezone_name)} '
                f'({TimeUtils.get_ddhhmmss_from_seconds(time.time() - tunnel.conns_inactive_at.timestamp())})'
            )

        lines.append(f'      ⛓️Tunnel 类型: {tunnel.tun_type}')
        lines.append(f'      🌐当前状态: {self._get_status_string(tunnel.status)}')
        return lines

    @classmethod
    def _get_status_string(cls, status: str) -> str:
        if status == 'degraded':
            return f'{cls._get_status_emoji(status)} 降级 DEGRADED'
        elif status == 'healthy':
            return f'{cls._get_status_emoji(status)} 正常 HEALTHY'
        elif status == 'down':
            return f'{cls._get_status_emoji(status)} 宕机 DOWN'
        elif status == 'inactive':
            return f'{cls._get_status_emoji(status)} 未连接过 INACTIVE'
        else:
            return f'{cls._get_status_emoji(status)} ERROR HASSEI!'  # Ptilospis!

    @staticmethod
    def _get_status_emoji(status: str) -> str:
        if status == 'degraded':
            return '⚠️'
        elif status == 'healthy':
            return '✅'
        elif status == 'down':
            return '⛔'
        elif status == 'inactive':
            return '❌'
        else:
            return '❓'

    async def active_tunnel_has_been_removed(self, umo_to_tunnels: Dict[str, List[str]]):
        curr_time = self.get_current_time()
        logger.debug(f'active_tunnel_has_been_removed is called ({curr_time})')

        for umo in umo_to_tunnels:
            msg_lines = ['💀 检测到远端移除了一个或多个 Tunnel']
            for tunnel_uuid in umo_to_tunnels[umo]:
                msg_lines.append(f'- {tunnel_uuid}')

            msg_lines.append('如以上情况并非您所为，请立即登录您的 CloudFlare 账号查看！')
            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            await self.send_func(umo, MessageChain().message("\n".join(msg_lines)))

    async def active_tunnel_has_down(self, umo_to_tunnels: Dict[str, List[str]],
                                     tunnels: Dict[str, TunnelStatusModel]):
        curr_time = self.get_current_time()
        logger.debug(f'active_tunnel_has_down is called {curr_time}')

        for umo in umo_to_tunnels:
            msg_lines = ['⛔ 监测到一个或多个 Tunnel 宕机/离线']
            for tunnel_uuid in umo_to_tunnels[umo]:
                logger.debug(f'active_tunnel_has_down: UUID {tunnel_uuid}')

                tunnel = tunnels[tunnel_uuid]

                if tunnel.conns_inactive_at is None:
                    # Error Handling
                    logger.warning(f'active_tunnel_has_down: no INACTIVE_AT data for {tunnel_uuid}')
                    msg_lines.extend([
                        f'🚇 {tunnel.name}',
                        f'      🏷️ID: {tunnel_uuid}',
                        '      于未知时间宕机/离线，建议手动查询'
                    ])
                else:
                    msg_lines.extend([
                        f'- {tunnel.name}',
                        f'   🏷️ID: {tunnel_uuid}',
                        f'   离线时间: {TimeUtils.get_datetime_strftime_in_tz(tunnel.conns_inactive_at, self.timezone_name)}'
                    ])

                msg_lines.append(self._get_status_string(tunnel.status))
            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            await self.send_func(umo, MessageChain().message("\n".join(msg_lines)))

    async def active_tunnel_has_degraded(self, umo_to_tunnels: Dict[str, List[str]],
                                         tunnels: Dict[str, TunnelStatusModel]):
        curr_time = self.get_current_time()
        logger.debug(f'active_tunnel_has_degraded is called {curr_time}')

        for umo in umo_to_tunnels:
            msg_lines = ['⚠️ 监测到一个或多个 Tunnel 降级']
            for tunnel_uuid in umo_to_tunnels[umo]:
                tunnel = tunnels[tunnel_uuid]
                logger.debug(f'active_tunnel_has_degraded: UUID {tunnel_uuid}')

                self._append_message_text_for_active_tunnel_info(tunnel, msg_lines)

            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            await self.send_func(umo, MessageChain().message("\n".join(msg_lines)))

    async def active_tunnel_has_active(self, umo_to_tunnels: Dict[str, List[str]],
                                       tunnels: Dict[str, TunnelStatusModel]):
        curr_time = self.get_current_time()
        logger.debug(f'active_tunnel_has_active is called {curr_time}')

        for umo in umo_to_tunnels:
            msg_lines = ['✅ 监测到一个或多个 Tunnel 上线/恢复正常']
            for tunnel_uuid in umo_to_tunnels[umo]:
                tunnel = tunnels[tunnel_uuid]
                logger.debug(f'active_tunnel_has_active: UUID {tunnel_uuid}')

                self._append_message_text_for_active_tunnel_info(tunnel, msg_lines)
            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            await self.send_func(umo, MessageChain().message("\n".join(msg_lines)))

    async def active_tunnel_has_conn_changed(self, umo_to_tunnels: Dict[str, List[str]],
                                             tunnels: Dict[str, TunnelStatusModel]):
        curr_time = self.get_current_time()
        logger.debug(f'active_tunnel_has_conn_changed is called {curr_time}')

        for umo in umo_to_tunnels:
            msg_lines = ['❗ 监测到一个或多个 Tunnel 的 连接数/Replica 数据有变化']
            for tunnel_uuid in umo_to_tunnels[umo]:
                tunnel = tunnels[tunnel_uuid]
                logger.debug(f'active_tunnel_has_conn_changed: UUID {tunnel_uuid}')

                self._append_message_text_for_active_tunnel_info(tunnel, msg_lines)
            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            await self.send_func(umo, MessageChain().message("\n".join(msg_lines)))

    def passive_append_tunnel_listing(self, tunnels: List[TunnelStatusModel]) -> List[str]:
        temp: List[str] = []
        for i in tunnels:
            if i.name == 'NoneNoneNoneNone':
                # 特殊处理尚未获取到信息的
                logger.debug(f'passive_append_tunnel_listing: tunnel {i} is uninitialized')

                temp.extend([
                    f'🚇 {i.name}',
                    f'      🏷️ID: {i.id}',
                    '      暂无信息'
                ])
                continue

            logger.debug(f'passive_append_tunnel_listing: tunnel {i}')
            temp.extend(self._append_message_text_for_tunnel_info_list(i))
        return temp


class NotificationManager:
    def __init__(self, cf_client: Cloudflare, account_id: str, basepath: str, polling_time: int,
                 sender: NotificationSender):
        self.cf_client = cf_client
        self.account_id = account_id
        self.basepath = basepath
        self.polling_time = polling_time
        self.sender = sender

        self.db_path = [os.path.join(self.basepath, 'notification_db.json'),
                        os.path.join(self.basepath, 'ignored_umo.json')]
        self.umo_to_tunnel: Dict[str, List[str]] = {}  # config
        self.ignored_umo: List[str] = []

        self.shared_lock = asyncio.Lock()

        self._load_notification_data()
        self._init_relation()

    def _load_notification_data(self):
        logger.info('加载既有 notification_db.json')
        self.umo_to_tunnel = FileUtils.load_json_with_default(self.db_path[0], {})
        logger.info('加载既有 ignored_umo.json')
        self.ignored_umo = FileUtils.load_json_with_default(self.db_path[1], [])

    def _save_notification_data(self):
        logger.debug('保存 notification_db.json')
        with open(self.db_path[0], "w", encoding="utf-8") as f:
            json.dump(self.umo_to_tunnel, f, indent=2, ensure_ascii=False)
        logger.debug('保存 ignored_umo.json')
        with open(self.db_path[1], "w", encoding="utf-8") as f:
            json.dump(self.ignored_umo, f, indent=2, ensure_ascii=False)

    def _list_all_tunnels(self):
        logger.debug('_list_all_tunnels is called')
        for i in range(5):
            try:
                logger.debug(f'_list_all_tunnels ({i}) calls CF API')
                temp = self.cf_client.zero_trust.tunnels.list(account_id=self.account_id)
            except RateLimitError:
                logger.warning(f'无法 list tunnels : 429 Rate Limit Error')
                raise CloudFlareAPI429Exception
            except APIError as e:
                logger.warning(f'无法 list tunnels : APIError for {e}')
                pass
            else:
                return temp

        logger.error('Unable to list all tunnels.')
        raise CloudFlareAPIRequestError

    def _init_relation(self):
        """ 从配置文件中生成 umo <-> tunnel 关系 """
        self.tunnel_to_umo: Dict[str, List[str]] = {}
        self.tunnel_status_cache: Dict[str, TunnelStatusModel] = OrderedDict()
        self.notification_status: Dict[str, Dict[str, TunnelStatusModel]] = {}
        for (umo, tunnels) in self.umo_to_tunnel.items():
            for tunnel in tunnels:
                # tunnel -> umo
                if tunnel not in self.tunnel_to_umo:
                    self.tunnel_to_umo[tunnel] = []
                self.tunnel_to_umo[tunnel].append(umo)

                # tunnel status
                if tunnel not in self.tunnel_status_cache:
                    self.tunnel_status_cache[tunnel] = TunnelStatusModel.get_default_values(tunnel)

                # notification status
                if tunnel not in self.notification_status:
                    self.notification_status[tunnel] = {}
                self.notification_status[tunnel][umo] = TunnelStatusModel.get_default_values(tunnel)

        self._polling_task = asyncio.create_task(self.polling_task_func())
        self._polling_is_429 = (False, 0.0)
        self._polling_last_run = 0

    async def add_relation(self, umo: str, tunnel: str):
        async with self.shared_lock:
            tunnel_uuid = self.get_tunnel_uuid(tunnel)

            if umo not in self.umo_to_tunnel:
                self.umo_to_tunnel[umo] = []
            if tunnel_uuid not in self.tunnel_to_umo:
                self.tunnel_to_umo[tunnel_uuid] = []
            if tunnel_uuid not in self.notification_status:
                self.notification_status[tunnel_uuid] = {}

            if tunnel_uuid not in self.umo_to_tunnel[umo] and umo not in self.tunnel_to_umo[tunnel_uuid]:
                self.umo_to_tunnel[umo].append(tunnel_uuid)
                self.tunnel_to_umo[tunnel_uuid].append(umo)

                self.tunnel_status_cache[tunnel_uuid] = TunnelStatusModel.get_default_values(tunnel_uuid)
                self.notification_status[tunnel_uuid][umo] = TunnelStatusModel.get_default_values(tunnel_uuid)
            else:
                raise TunnelAlreadyAddedException

            self._save_notification_data()

    async def remove_relation(self, umo: str, tunnel: str):
        async with self.shared_lock:
            tunnel_uuid = self.get_tunnel_uuid(tunnel)

            # 首先检查这个 umo 和 tunnel 是有数据的（能够访问）
            if umo not in self.umo_to_tunnel and tunnel_uuid not in self.tunnel_to_umo and tunnel_uuid not in self.notification_status and tunnel_uuid not in self.tunnel_status_cache:
                raise TunnelAlreadyRemovedException
            # 然后检查 umo 和 tunnel 各自存不存在
            if umo not in self.tunnel_to_umo[tunnel_uuid] and umo not in self.umo_to_tunnel[
                tunnel_uuid] and umo not in \
                    self.notification_status[tunnel_uuid]:
                raise TunnelAlreadyRemovedException

            if umo in self.umo_to_tunnel[tunnel_uuid]:
                self.umo_to_tunnel[tunnel_uuid].remove(umo)
            if tunnel_uuid in self.tunnel_to_umo[umo]:
                self.tunnel_to_umo[umo].remove(tunnel_uuid)
            if tunnel_uuid in self.notification_status:
                if umo in self.notification_status[tunnel_uuid]:
                    del self.notification_status[tunnel_uuid][umo]

            # 最后删掉不要的
            if len(self.tunnel_to_umo[umo]) == 0:
                del self.umo_to_tunnel[umo]
                del self.tunnel_status_cache[tunnel_uuid]
                del self.notification_status[tunnel_uuid]
            if len(self.umo_to_tunnel[tunnel_uuid]) == 0:
                del self.tunnel_to_umo[tunnel_uuid]

            self._save_notification_data()

    async def remove_tunnel(self, tunnel: str, has_acquired_lock: bool = False) -> List[str]:
        """单方面移除掉一个tunnel，解除其与所有umo的关系（如：远端CF配置改变）"""
        if not has_acquired_lock:
            await self.shared_lock.acquire()

        tunnel_uuid = self.get_tunnel_uuid(tunnel)
        temp = await self.remove_tunnel_by_uuid(tunnel_uuid, True)

        self._save_notification_data()

        if not has_acquired_lock:
            self.shared_lock.release()

        return temp

    async def remove_tunnel_by_uuid(self, tunnel_uuid: str, has_acquired_lock: bool = False) -> List[str]:
        if not has_acquired_lock:
            await self.shared_lock.acquire()

        umos = self.tunnel_to_umo.pop(tunnel_uuid, [])
        for umo in umos:
            self.umo_to_tunnel[tunnel_uuid].remove(umo)

        del self.tunnel_status_cache[tunnel_uuid]
        del self.notification_status[tunnel_uuid]

        return umos

    async def remove_umo(self, umo: str) -> List[str]:
        """单方面移除掉一个umo，解除其与所有tunnel的关系"""
        async with self.shared_lock:
            # relations
            tunnels = self.umo_to_tunnel.pop(umo, [])
            for tunnel in tunnels:
                self.tunnel_to_umo[tunnel].remove(umo)
                del self.notification_status[tunnel][umo]

                if len(self.tunnel_to_umo[tunnel]) == 0:
                    del self.tunnel_to_umo[tunnel]
                    del self.tunnel_status_cache[tunnel]
                    del self.notification_status[tunnel]

            self._save_notification_data()

            return tunnels

    async def reset(self):
        """ok wtf"""
        async with self.shared_lock:
            self.umo_to_tunnel = {}
            self.tunnel_to_umo = {}
            self.tunnel_status_cache = OrderedDict()
            self.notification_status = {}
            self._save_notification_data()

            self.terminate_task()

            self._polling_task = asyncio.create_task(self.polling_task_func())
            self._polling_last_run = 0

    async def add_ignored_umo(self, umo: str):
        async with self.shared_lock:
            if umo not in self.ignored_umo:
                self.ignored_umo.append(umo)

            self._save_notification_data()

    async def remove_ignored_umo(self, umo: str):
        async with self.shared_lock:
            if umo in self.ignored_umo:
                self.ignored_umo.remove(umo)

            self._save_notification_data()

    def get_all_tunnel_ids(self) -> List[Dict[str, str]]:
        result = self._list_all_tunnels()

        uuid_to_name = {}
        name_to_uuid = {}

        for i in result:
            uuid_to_name[i.id] = i.name
            name_to_uuid[i.name] = i.id

        return [uuid_to_name, name_to_uuid]

    def get_tunnel_uuid(self, tunnel: str) -> str:
        def get_tunnel_data():
            if not self._polling_is_429[0]:
                return self.get_all_tunnel_ids()
            else:
                return None

        try:
            UUID(tunnel)
        except Exception:
            tunnel_data = get_tunnel_data()

            if tunnel_data is None:
                # 暂时无法创建对应的 name -> uuid mapping
                raise CloudFlareAPI429Exception

            if tunnel not in tunnel_data[1]:
                raise TunnelNotFoundException
            else:
                return tunnel_data[1][tunnel]
        else:
            if tunnel in self.tunnel_to_umo:
                # 是已知的 tunnel
                return tunnel

            tunnel_data = get_tunnel_data()

            if tunnel_data is not None:
                if tunnel not in tunnel_data[0]:
                    raise TunnelNotFoundException
                else:
                    return tunnel
            else:
                # 前面已经检查过了，所以行进到这里的话只能抛429了
                raise CloudFlareAPI429Exception

    async def get_cached_tunnel_status(self, tunnel: str) -> TunnelStatusModel:
        async with self.shared_lock:
            tunnel_uuid = self.get_tunnel_uuid(tunnel)

            if tunnel_uuid not in self.tunnel_status_cache:
                raise TunnelNotFoundException
            else:
                return self.tunnel_status_cache[tunnel_uuid]

    async def update_and_send_tunnel_status(self):
        if self._polling_is_429[0]:
            if time.time() - self._polling_is_429[1] <= 300:
                # 5 minute blocking
                raise CloudFlareAPI429Exception
            else:
                self._polling_is_429 = (False, 0.0)

        async with self.shared_lock:
            try:
                all_tunnels = self._list_all_tunnels()
            except CloudFlareAPI429Exception:
                self._polling_is_429 = (True, time.time())
                raise
            except CloudFlareAPIRequestError:
                raise

            # 先检查有没有tunnel已经被移除掉的
            deleted_uuid = TunnelStatusUtils.find_deleted_tunnels(list(self.tunnel_status_cache.values()),
                                                                  all_tunnels)
            deleted_pairs = {}
            if len(deleted_uuid) > 0:
                deleted_pairs = TunnelStatusUtils.pair_umo_and_tunnelid_by_tunnel_ids(self.tunnel_to_umo, deleted_uuid,
                                                                                      self.ignored_umo)

                for i in deleted_uuid:
                    await self.remove_tunnel(i, True)

            # 然后，获取所有tunnel的新数据
            old_tunnels = self.tunnel_status_cache
            new_tunnels: Dict[str, TunnelStatusModel] = OrderedDict()
            for i in all_tunnels:
                if i.id not in old_tunnels:
                    continue
                new_tunnels[i.id] = TunnelStatusModel.create_from_tunnel_entry(i)
            # 比较新旧tunnel数据
            diffs = TunnelStatusUtils.calc_status_difference(old_tunnels, new_tunnels)

            # 替换旧的为新的数据
            self.tunnel_status_cache = new_tunnels

        # 发送的时候释放
        # 逐个发送变化通知
        await self.sender.active_tunnel_has_been_removed(deleted_pairs)
        await self.sender.active_tunnel_has_active(
            TunnelStatusUtils.pair_umo_and_tunnelid_by_tunnel_ids(self.tunnel_to_umo, diffs["to_healthy"],
                                                                  self.ignored_umo),
            new_tunnels)
        await self.sender.active_tunnel_has_degraded(
            TunnelStatusUtils.pair_umo_and_tunnelid_by_tunnel_ids(self.tunnel_to_umo, diffs["to_degraded"],
                                                                  self.ignored_umo),
            new_tunnels)
        await self.sender.active_tunnel_has_down(
            TunnelStatusUtils.pair_umo_and_tunnelid_by_tunnel_ids(self.tunnel_to_umo, diffs["to_down"],
                                                                  self.ignored_umo),
            new_tunnels
        )
        await self.sender.active_tunnel_has_conn_changed(
            TunnelStatusUtils.pair_umo_and_tunnelid_by_tunnel_ids(self.tunnel_to_umo, diffs["conn_changed"],
                                                                  self.ignored_umo),
            new_tunnels
        )

        self._polling_last_run = time.time()

    async def polling_task_func(self):
        try:
            while True:
                try:
                    logger.info("正在更新与发送 Tunnel 状态……")
                    await self.update_and_send_tunnel_status()
                except CloudFlareAPI429Exception:
                    logger.error(
                        f"触发 CloudFlare API 429 速率限制，当前时间 {TimeUtils.get_current_strftime_utc()}")
                    await asyncio.sleep(310)  # 等待 300 + 10 秒
                except CloudFlareAPIRequestError:
                    logger.warning(f"CloudFlare API 请求失败，等待 10 秒后重试")
                    await asyncio.sleep(10)
                    continue

                logger.info("已完成更新与发送 Tunnel 状态")
                await asyncio.sleep(self.polling_time)
        except asyncio.CancelledError:
            logger.info("已取消更新轮询任务")

    def terminate_task(self):
        self._polling_task.cancel()

    @property
    def last_update_time(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self._polling_last_run, datetime.UTC)


@register("astrbot_plugin_cloudflare_tunnel_monitor", "sctop",
          "一个基本算是自用的 CloudFlare Tunnel 存活状态的监测插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.data_basepath = os.path.join(get_astrbot_data_path(), "plugin_data", self.name)

        # create base folder for the plugin
        os.makedirs(self.data_basepath, exist_ok=True)

        self.has_initialized = False

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        if self.config.get('api_token') == '' and self.config.get('account_id'):
            logger.error('请首先配置 `api_token` 与 `account_id`，然后重启插件使用！')
            return
        self.has_initialized = True

        if self.config.get('http_proxy') != '':
            self.cf_client = Cloudflare(api_token=self.config.get('api_token'),
                                        http_client=DefaultHttpxClient(proxy=self.config.get('http_proxy')))
        else:
            self.cf_client = Cloudflare(api_token=self.config.get('api_token'))

        self.notification_sender = NotificationSender(self.send_message_callback, self.config.get('time_timezone'))
        self.notification_manager = NotificationManager(self.cf_client, self.config.get('account_id'),
                                                        self.data_basepath,
                                                        self.config.get('polling_time'),
                                                        sender=self.notification_sender)

    def __check_has_inited(self):
        if not self.has_initialized:
            raise RuntimeError('Hasn\'t been initialized!')

    @filter.command_group("cft")
    async def cft(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command('on')
    async def cft_on(self, event: AstrMessageEvent, target_umo: str = None):
        """启用本 umo 聊天，或指定一个 umo 聊天，的【主动】推送功能"""
        try:
            self.__check_has_inited()

            temp = target_umo if target_umo is not None else event.unified_msg_origin
            await self.notification_manager.remove_ignored_umo(temp)

            yield event.plain_result(f'✅ 已成功启用 `{temp}` 的主动推送功能！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command('off')
    async def cft_off(self, event: AstrMessageEvent, target_umo: str = None):
        """关闭本 umo 聊天，或指定一个 umo 聊天，的【主动】推送功能"""
        try:
            self.__check_has_inited()

            temp = target_umo if target_umo is not None else event.unified_msg_origin
            await self.notification_manager.add_ignored_umo(temp)

            yield event.plain_result(f'✅ 已成功关闭 `{temp}` 的主动推送功能！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("add")
    async def add_tunnel(self, event: AstrMessageEvent, name: str):
        try:
            self.__check_has_inited()

            await self.notification_manager.add_relation(event.unified_msg_origin, name)
            yield event.plain_result(f'✅ 添加 `{name}` 成功！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("remove")
    async def remove(self, event: AstrMessageEvent, name: str):
        try:
            self.__check_has_inited()

            await self.notification_manager.remove_relation(event.unified_msg_origin, name)
            yield event.plain_result(f'✅ 移除 `{name}` 成功！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @cft.command("list")
    async def list(self, event: AstrMessageEvent):
        try:
            self.__check_has_inited()

            curr_list = self.notification_manager.umo_to_tunnel.get(event.unified_msg_origin, [])
            curr_tunnels = [self.notification_manager.tunnel_status_cache[i] for i in curr_list]

            msg_lines = [
                '🔍 以下是正在监测的 Tunnels 信息'
            ]
            msg_lines.extend(self.notification_sender.passive_append_tunnel_listing(curr_tunnels))

            msg_lines.append('')
            msg_lines.append(
                f'📦缓存更新时间: {TimeUtils.get_datetime_strftime_in_tz(self.notification_manager.last_update_time, self.config.get("time_timezone"))}'
            )
            msg_lines.append(f'🕙当前时间: {self.notification_sender.get_current_time()}')

            yield event.plain_result("\n".join(msg_lines))
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @cft.command("list_all_tunnels")
    async def list_all(self, event: AstrMessageEvent):
        """这里指的是列出所有正在监控的 Tunnels"""
        try:
            self.__check_has_inited()

            msg_lines = [
                '🔍 以下是全局正在监测的 Tunnels 信息'
            ]
            msg_lines.extend(
                self.notification_sender.passive_append_tunnel_listing(
                    list(self.notification_manager.tunnel_status_cache.values())
                )
            )
            msg_lines.append('')

            msg_lines.append('📋 以下是 UMO -> Tunnel UUID 信息')
            for (umo, tunnels) in self.notification_manager.umo_to_tunnel.items():
                msg_lines.append(f'- {umo}')
                for tunnel in tunnels:
                    msg_lines.append(f'   - {tunnel}')
            msg_lines.append('')

            msg_lines.append('📋 以下是 Tunnel UUID -> UMO 信息')
            for (tunnel, umos) in self.notification_manager.tunnel_to_umo.items():
                msg_lines.append(f'- {tunnel}')
                for umo in umos:
                    msg_lines.append(f'   - {umo}')
            msg_lines.append('')

            msg_lines.append(
                f'📦缓存更新时间: {TimeUtils.get_datetime_strftime_in_tz(self.notification_manager.last_update_time, self.config.get("time_timezone"))}'
            )
            msg_lines.append(f'🕙当前时间: {self.notification_sender.get_current_time()}')

            yield event.plain_result("\n".join(msg_lines))
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @cft.command("list_all_tunnels_api")
    async def list_api_all(self, event: AstrMessageEvent):
        """这里指的是列出整个 API Key 下面都可以用于监测的 Tunnels"""
        try:
            self.__check_has_inited()

            all_tunnels = [TunnelStatusModel.create_from_tunnel_entry(i)
                           for i in self.notification_manager._list_all_tunnels()]
            curr_time = self.notification_sender.get_current_time()

            msg_lines = [
                '🔍 以下是账号中所有可用于添加的 Tunnels'
            ]
            msg_lines.extend(self.notification_sender.passive_append_tunnel_listing(all_tunnels))

            msg_lines.append('')
            msg_lines.append(f'🕙当前时间: {curr_time}')

            yield event.plain_result("\n".join(msg_lines))
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("clear")
    async def clear(self, event: AstrMessageEvent):
        """将当前聊天的所有tunnel监听任务给爆了"""
        try:
            self.__check_has_inited()

            await self.notification_manager.remove_umo(event.unified_msg_origin)
            yield event.plain_result(f'✅ 清空当前 `{event.unified_msg_origin}` 的所有监控任务成功！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("force_update")
    async def force_update(self, event: AstrMessageEvent):
        """强制调用更新函数"""
        try:
            self.__check_has_inited()

            await self.notification_manager.update_and_send_tunnel_status()
            yield event.plain_result(f'✅ 强制调用更新函数完成！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("reset")
    async def reset(self, event: AstrMessageEvent):
        """将所有聊天的所有tunnel监听任务都给爆了"""
        try:
            self.__check_has_inited()

            await self.notification_manager.reset()
            yield event.plain_result(f'✅ 重置所有数据完成！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("remove_umo")
    async def remove_umo(self, event: AstrMessageEvent, target_umo: str):
        try:
            self.__check_has_inited()

            await self.notification_manager.remove_umo(target_umo)
            yield event.plain_result(f'✅ 移除指定 `{target_umo}` 完成！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @cft.command("remove_tunnel")
    async def remove_tunnel(self, event: AstrMessageEvent, target_tunnel: str):
        try:
            self.__check_has_inited()

            await self.notification_manager.remove_tunnel(target_tunnel)
            yield event.plain_result(f'✅ 移除 Tunnel `{target_tunnel}` 的监控任务完成！')
        except Exception as e:
            yield event.plain_result(f'🚨 执行失败！请稍后重试。\n失败原因：{e}')
        finally:
            event.stop_event()

    async def send_message_callback(self, umo: str, message_chain: MessageChain):
        for i in range(10):
            try:
                await self.context.send_message(umo, message_chain)
                return
            except Exception as e:
                logger.error(f'无法发送消息到 {umo} (第{i + 1}次 ,MessageChain {message_chain}): {e}')
        logger.error(f'无法发送信息到 {umo} ，已取消本次发送')

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.notification_manager.terminate_task()
