"""单用户桌面创作工作台；网络任务在后台执行，主线程只处理界面。"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from kantoku.config import KantokuError, get_settings
from kantoku.config.settings import ROOT
from kantoku.core import budget
from kantoku.perception.qc import qc_image
from kantoku.schemas.media import ImageGenerationResult
from kantoku.schemas.qc import QcResult
from kantoku.shells.image_cli import _money_fen, _provider
from kantoku.tools.studio import (
    StudioTask,
    compose_prompt,
    create_task,
    execute_task,
    list_tasks,
    recover_task,
    refine_prompt,
    task_directory,
)


class Studio:
    """将预览、付费确认、恢复、图片查看汇入一个窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.busy = False
        self.tasks: list[StudioTask] = []
        self.task: StudioTask | None = None
        self.result: ImageGenerationResult | None = None
        self.photo: tk.PhotoImage | None = None
        self.events: queue.Queue[tuple[bool, object]] = queue.Queue()
        root.title("监督酱 · 创作工作台")
        root.geometry("1100x820")
        root.minsize(900, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="从第一张可用作品开始", font=("Microsoft YaHei", 20)).pack(anchor="w")
        ttk.Label(panel, text="① 填需求 → ② 编辑提示词 → ③ 核价并确认 → ④ 看图与保存").pack(
            anchor="w"
        )
        panes = ttk.Panedwindow(panel, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=12)
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=1)
        self.project = self.entry(left, "作品名称（同一作品返工时保持一致）", "我的第一份作品")
        self.shot = self.entry(left, "镜号 / 画面编号（同一画面返工保持一致）", "1")
        self.purpose = self.entry(left, "用途", "宣传海报底图")
        self.audience = self.entry(left, "想打动谁", "喜欢自然、有生活气息画面的用户")
        self.style = self.entry(left, "画风 / 摄影方向", "自然光，主体清晰，克制的色彩")
        ttk.Label(left, text="画面内容：主体、场景、情绪、要传达什么").pack(anchor="w")
        self.subject = tk.Text(left, height=4, width=46, wrap="word")
        self.subject.pack(fill="x")
        ttk.Button(left, text="整理为提示词（免费）", command=self.compose).pack(fill="x", pady=5)
        ttk.Label(left, text="最终提示词（可直接粘贴自己的完整提示词）").pack(anchor="w")
        self.prompt = tk.Text(left, height=8, width=46, wrap="word")
        self.prompt.pack(fill="both", expand=True)
        ttk.Button(left, text="让 AI 优化设计（调用文本模型）", command=self.refine).pack(fill="x")
        self.price = self.entry(left, "已核实的 API 单次费用上界（元，必填）", "")
        ttk.Label(left, text="客户端积分不等于 API 价格；每次只生成一张。", wraplength=430).pack(
            anchor="w"
        )
        self.generate_button = ttk.Button(left, text="确认费用并生成一张", command=self.generate)
        self.generate_button.pack(fill="x", pady=6)
        ttk.Label(right, text="历史任务（点击查看，重新打开仍能恢复）").pack(anchor="w")
        self.history = tk.Listbox(right, height=6, exportselection=False)
        self.history.pack(fill="x")
        self.history.bind("<<ListboxSelect>>", self.select)
        ttk.Button(right, text="查询所选任务（不重新生成）", command=self.recover).pack(fill="x")
        ttk.Button(right, text="继续所选未提交任务（先确认费用）", command=self.resume).pack(
            fill="x"
        )
        self.preview = ttk.Label(right, text="生成的图片会显示在这里", anchor="center")
        self.preview.pack(fill="both", expand=True, pady=10)
        ttk.Button(right, text="另存图片到交付文件夹", command=self.export).pack(fill="x")
        ttk.Button(right, text="打开原图 / 外部查看器", command=self.open_image).pack(fill="x")
        ttk.Button(right, text="打开创作记录文件夹", command=self.open_folder).pack(fill="x")
        ttk.Button(right, text="视觉预筛（调用视觉模型）", command=self.quality_check).pack(
            fill="x"
        )
        ttk.Button(right, text="填写已核实账单 / 查看预算", command=self.settle).pack(fill="x")
        self.status = tk.StringVar(value="就绪。请先填写需求。打开工作台不会调用模型。")
        ttk.Label(panel, textvariable=self.status, wraplength=1020).pack(fill="x")
        ttk.Button(panel, text="检查模型配置（不联网）", command=self.preflight).pack(anchor="e")
        self.refresh()
        root.after(150, self.poll)

    def entry(self, parent: ttk.Frame, label: str, value: str) -> tk.StringVar:
        ttk.Label(parent, text=label).pack(anchor="w", pady=(5, 0))
        variable = tk.StringVar(value=value)
        ttk.Entry(parent, textvariable=variable).pack(fill="x")
        return variable

    def preflight(self) -> None:
        if self.busy:
            return
        try:
            settings = get_settings()
            lines = [
                f"生图：{settings.image.model} · {settings.image.width} × {settings.image.height}",
                f"文本设计：{settings.llm.model_chat}",
                f"视觉预筛：{settings.llm.model_vision}",
                f"单作品生图预算：¥{settings.budget.image_project_cny}",
            ]
            for label, check in (
                (
                    "生图凭据",
                    lambda: _provider().validate_request(
                        prompt="preflight",
                        shot_no=1,
                        reference_urls=(),
                        seed=None,
                    ),
                ),
                ("文本凭据", settings.llm.chat_api_key),
                ("视觉凭据", settings.llm.vision_api_key),
            ):
                try:
                    check()
                    lines.append(f"{label}：本地配置就绪")
                except KantokuError:
                    lines.append(f"{label}：配置缺失或无效，请检查根目录 .env")
            lines.append("本地自检不代表供应商服务验证通过；真实请求须单独确认。")
            messagebox.showinfo("当前配置", "\n".join(lines))
        except KantokuError as error:
            self.error(error)

    def compose(self) -> None:
        try:
            text = compose_prompt(
                self.subject.get("1.0", "end"),
                self.purpose.get(),
                self.audience.get(),
                self.style.get(),
            )
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", text)
        except KantokuError as error:
            messagebox.showerror("请补充需求", str(error))

    def refresh(self) -> None:
        try:
            self.tasks = list_tasks()
            self.history.delete(0, "end")
            for task in self.tasks:
                self.history.insert(
                    "end", f"{task.project} · 镜{task.shot_no} · {task.request_id[-8:]}"
                )
        except KantokuError as error:
            self.status.set(str(error))

    def select(self, event: tk.Event | None = None) -> None:
        if self.busy or not self.history.curselection():
            return
        self.task = self.tasks[self.history.curselection()[0]]
        self.project.set(self.task.project)
        self.shot.set(str(self.task.shot_no))
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", self.task.prompt)
        try:
            self.show_result(budget.load_generation_result(self.task.request_id))
        except KantokuError as error:
            self.error(error)

    def refine(self) -> None:
        if self.busy:
            return
        brief = self.prompt.get("1.0", "end").strip()
        if not brief:
            self.compose()
            brief = self.prompt.get("1.0", "end").strip()
        if brief and messagebox.askyesno(
            "确认文本模型调用",
            "将当前提示词发送到配置的文本模型进行设计优化。\n"
            "按你的模型供应商计费，文本费用不包含在生图台账内。继续吗？",
        ):
            self.start(lambda: refine_prompt(brief, confirmed=True))

    def quality_check(self) -> None:
        if self.busy or self.result is None or self.result.path is None:
            return
        path = self.result.path
        purpose, audience, style = self.purpose.get(), self.audience.get(), self.style.get()
        if messagebox.askyesno(
            "确认视觉模型调用",
            "将所选图片发送到配置的视觉模型。\n"
            "视觉费用不包含在生图台账内；结果仅作建议，最终由你判断。继续吗？",
        ):
            self.start(
                lambda: qc_image(
                    path,
                    target_platform=purpose,
                    genre=purpose,
                    target_audience=audience,
                    cinematography_requirements=style,
                    visual_style=style,
                    confirm_paid=True,
                )
            )

    def settle(self) -> None:
        if self.busy or self.task is None:
            return
        try:
            summary = budget.summarize_budget(self.task.project)
            text = simpledialog.askstring(
                "账单核对",
                f"已结算：{summary.settled_fen} 分；预占：{summary.held_fen} 分\n"
                f"项目剩余：{summary.available_fen} 分\n"
                "输入所选任务在供应商的最终实扣金额（元）；取消仅查看。\n"
                "只有平台已确认免费的任务才填写 0。",
            )
            if text is not None:
                amount = _money_fen(text)
                if messagebox.askyesno("确认账单", f"确认原任务最终实扣为 {amount} 分？"):
                    budget.settle(self.task.request_id, amount)
                    self.status.set(f"账单已记录：{amount} 分。")
        except Exception as error:
            self.error(error)

    def confirm(self, project: str, estimate: int) -> bool:
        summary = budget.summarize_budget(project)
        return messagebox.askyesno(
            "确认真实 API 调用",
            f"作品：{project}\n本次生成：1 张\n"
            f"你核实的费用上界：¥{estimate / 100:.2f}\n"
            f"已结算 ¥{summary.settled_fen / 100:.2f}；预占 ¥{summary.held_fen / 100:.2f}\n"
            "确认将此提示词发送给已配置的生图供应商？\n"
            "预算仍会再次校验；未知账单保持预占。",
        )

    def generate(self) -> None:
        if self.busy:
            return
        try:
            estimate = _money_fen(self.price.get())
            if estimate <= 0:
                raise ValueError("请填写大于零的核实费用上界")
            shot = int(self.shot.get())
            prompt = self.prompt.get("1.0", "end").strip()
            provider = _provider()
            provider.validate_request(prompt=prompt, shot_no=shot, reference_urls=(), seed=None)
            if not self.project.get().strip():
                raise ValueError("请填写作品名称")
            if not self.confirm(self.project.get().strip(), estimate):
                return
            self.task = create_task(self.project.get(), prompt, shot, estimate)
            task = self.task
            self.refresh()
            self.start(lambda: execute_task(task, provider=provider, confirmed=True))
        except Exception as error:
            self.error(error)

    def resume(self) -> None:
        if self.busy or self.task is None:
            return
        try:
            task = self.task
            record = budget.get_reservation(task.request_id)
            if record is not None and record.status != "reserved":
                self.recover()
                return
            provider = _provider()
            if self.confirm(task.project, task.estimate_fen):
                self.start(lambda: execute_task(task, provider=provider, confirmed=True))
        except Exception as error:
            self.error(error)

    def recover(self) -> None:
        if self.busy or self.task is None:
            return
        try:
            task, provider = self.task, _provider()
            self.start(lambda: recover_task(task, provider=provider))
        except Exception as error:
            self.error(error)

    def start(self, action: Callable[[], ImageGenerationResult | str | QcResult]) -> None:
        self.busy = True
        self.generate_button.state(["disabled"])
        self.status.set("正在处理任务，请等待。任务 ID 已保存；中断后请查询原任务。")

        def worker() -> None:
            try:
                self.events.put((True, action()))
            except Exception as error:
                self.events.put((False, error))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self) -> None:
        try:
            success, payload = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.generate_button.state(["!disabled"])
            if success and isinstance(payload, ImageGenerationResult):
                self.show_result(payload)
            elif success and isinstance(payload, str):
                self.prompt.delete("1.0", "end")
                self.prompt.insert("1.0", payload)
                self.status.set("AI 设计完成。请检查并编辑提示词，再确认生图费用。")
            elif success and isinstance(payload, QcResult):
                self.status.set(f"视觉预筛建议：{payload.reason}（最终由你判断）")
                messagebox.showinfo("视觉预筛", payload.reason)
            elif isinstance(payload, Exception):
                self.error(payload)
        self.root.after(150, self.poll)

    def show_result(self, result: ImageGenerationResult | None) -> None:
        self.result = result
        self.photo = None
        self.preview.configure(image="", text="尚无可查看的图片")
        if result is None:
            self.status.set("任务已保存，可继续未提交任务，或查询已提交任务。")
            return
        state = {"succeeded": "生成成功", "failed": "生成失败", "unknown": "等待查询或对账"}
        cost = "待对账（保留预占）" if result.actual_fen is None else f"{result.actual_fen} 分"
        self.status.set(f"{state[result.status]}；实际费用：{cost}。{result.error or ''}")
        if result.path is not None and result.path.is_file():
            try:
                photo = tk.PhotoImage(file=str(result.path.resolve()))
                factor = max(1, (photo.width() + 429) // 430, (photo.height() + 379) // 380)
                self.photo = photo.subsample(factor)
                self.preview.configure(image=self.photo, text="")
            except tk.TclError:
                self.preview.configure(text="图片已保存，点击“打开原图”查看")

    def export(self) -> None:
        if self.result is None or self.result.path is None or not self.result.path.is_file():
            messagebox.showinfo("尚无图片", "生成成功后才能另存交付图片。")
            return
        source = self.result.path.resolve()
        target = filedialog.asksaveasfilename(
            initialfile=source.name,
            defaultextension=source.suffix,
            filetypes=[("图片", "*" + source.suffix)],
        )
        if target:
            try:
                if Path(target).resolve() != source:
                    shutil.copy2(source, target)
                self.status.set(f"图片已保存：{target}。请人工检查主体、细节、文字和使用授权。")
            except OSError as error:
                self.error(error)

    def open_image(self) -> None:
        if self.result is not None and self.result.path is not None:
            self.open_path(self.result.path.resolve())

    def open_folder(self) -> None:
        try:
            directory = task_directory()
            directory.mkdir(parents=True, exist_ok=True)
            self.open_path(directory)
        except OSError as error:
            self.error(error)

    def open_path(self, path: Path) -> None:
        try:
            os.startfile(path)
        except OSError as error:
            self.error(error)

    def error(self, error: Exception) -> None:
        message = (
            str(error)
            if isinstance(error, (KantokuError, ValueError))
            else (f"操作未完成（{type(error).__name__}），请检查输入、目录权限或配置。")
        )
        self.status.set(message)
        messagebox.showerror("操作未完成", message)

    def close(self) -> None:
        if self.busy:
            messagebox.showinfo("任务处理中", "请等待本次请求返回，再关闭窗口。")
            return
        self.root.destroy()


def main() -> None:
    """从任意工作目录启动，沿用项目配置和密钥。"""
    os.chdir(ROOT)
    root = tk.Tk()
    try:
        get_settings()
        Studio(root)
    except KantokuError as error:
        messagebox.showerror("配置需要处理", str(error))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
