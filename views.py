"""참가하기 버튼과 대회 공지 임베드.

버튼은 custom_id 에 ctf_id 를 담는 DynamicItem 이라, 봇을 재시작해도
과거에 올린 공지의 버튼이 그대로 작동한다 (뷰 상태를 따로 저장할 필요 없음).
"""

from __future__ import annotations

from datetime import datetime

import discord

import db  # noqa: F401  (post_announcement 이 공지 위치를 저장한다)


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ctf_join:(?P<ctf_id>\d+)"):
    def __init__(self, ctf_id: int) -> None:
        self.ctf_id = ctf_id
        super().__init__(
            discord.ui.Button(
                label="참가할게",
                style=discord.ButtonStyle.primary,
                emoji="🖐️",
                custom_id=f"ctf_join:{ctf_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["ctf_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await db.get_ctf(self.ctf_id)
        if ctf is None:
            await interaction.followup.send("이 대회 정보를 못 찾겠어.", ephemeral=True)
            return
        if ctf["status"] != "active":
            await interaction.followup.send("이미 끝난 대회야.", ephemeral=True)
            return

        role = interaction.guild.get_role(ctf["role_id"])
        if role is None:
            await interaction.followup.send("참가 역할이 사라졌어.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.followup.send(
                f"이미 **{ctf['name']}** 에 참가 중이야.", ephemeral=True
            )
            return

        try:
            await interaction.user.add_roles(role, reason="CTF 참가 (버튼)")
        except discord.Forbidden:
            await interaction.followup.send(
                "봇 권한이 부족해서 역할을 못 줬어. 서버 설정 > 역할 에서 "
                "봇 역할을 대회 역할보다 **위로** 올려줘.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"**{ctf['name']}** 참가 완료! 이제 대회 채널이 보일 거야.", ephemeral=True
        )


class ChallengeJoinButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"ctf_thread:(?P<thread_id>\d+)"
):
    """문제 스레드에 참여시키는 버튼.

    디스코드는 "내가 멤버인 스레드"만 사이드바에 들여써서 보여준다. 그래서 이 버튼이
    눌린 사람만 스레드에 넣어준다 (전원 자동 참여시키지 않는다).
    """

    def __init__(self, thread_id: int) -> None:
        self.thread_id = thread_id
        super().__init__(
            discord.ui.Button(
                label="참가하기",
                style=discord.ButtonStyle.secondary,
                emoji="🧵",
                custom_id=f"ctf_thread:{thread_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["thread_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        thread = interaction.guild.get_channel_or_thread(self.thread_id)
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "이 문제 스레드가 사라졌어.", ephemeral=True
            )
            return

        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            await interaction.response.send_message(
                "스레드에 넣지 못했어. 권한을 확인해줘.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"{thread.mention} 참여했어. 왼쪽 목록에서 바로 보일 거야.", ephemeral=True
        )


def challenge_view(thread_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ChallengeJoinButton(thread_id))
    return view


def announcement_text(
    name: str, start: datetime | None = None, end: datetime | None = None
) -> str:
    lines = [f"다음 대회 : **{name}**"]
    if start is not None or end is not None:
        parts = [discord.utils.format_dt(t, "f") for t in (start, end) if t is not None]
        schedule = " ~ ".join(parts)
        if start is not None and end is not None:
            hours = round((end - start).total_seconds() / 3600)
            schedule += f"({hours}h)"
        lines.append(f"일정 : {schedule}")
    return "\n".join(lines)


def join_view(ctf_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JoinButton(ctf_id))
    return view


class ConfirmDelete(discord.ui.View):
    """되돌릴 수 없는 삭제 전에 한 번 물어보는 확인 창.

    디스코드 채널 삭제는 복구가 불가능하므로, 무엇이 사라지는지 보여주고
    명령을 실행한 본인만 확정할 수 있게 한다.
    """

    def __init__(self, user_id: int, *, label: str = "삭제") -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed: bool | None = None
        self.confirm.label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "이건 명령을 실행한 사람만 누를 수 있어.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        await interaction.response.defer()
        self.stop()


async def post_announcement(
    channel: discord.abc.GuildChannel,
    ctf_id: int,
    name: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    """공지 채널에 대회 공지 + 참가 버튼을 올린다. 포럼 채널이면 게시글로 만든다.

    나중에 일정이 바뀌면 이 메시지를 고쳐 쓸 수 있도록 위치를 DB에 기억해둔다.
    """
    text = announcement_text(name, start, end)
    view = join_view(ctf_id)

    if isinstance(channel, discord.ForumChannel):
        thread_with_message = await channel.create_thread(
            name=f"다음 대회 : {name}", content=text, view=view
        )
        message = thread_with_message.message
    else:
        message = await channel.send(content=text, view=view)

    await db.set_notice_message(ctf_id, message.channel.id, message.id)


async def update_announcement(
    guild: discord.Guild,
    ctf,
    start: datetime | None = None,
    end: datetime | None = None,
) -> bool:
    """일정이 바뀌었을 때 이미 올라간 공지를 고쳐 쓴다. 성공하면 True."""
    channel_id, message_id = ctf["notice_channel_id"], ctf["notice_message_id"]
    if not channel_id or not message_id:
        return False

    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        return False

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(content=announcement_text(ctf["name"], start, end))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False
    return True
