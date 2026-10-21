<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'

/**
 * 自定义 Dropdown：替代浏览器原生 <select>。
 * 原生菜单在 Windows/Chrome 下由系统绘制，圆角、边框、阴影与选中态都无法控制。
 * 这里只负责呈现，业务取值仍由 v-model 决定。
 */
export interface DropdownOption<T extends string> { value: T; label: string; hint?: string }

const props = withDefaults(defineProps<{
  modelValue: string
  options: { value: string; label: string; hint?: string }[]
  label?: string
  disabled?: boolean
  placeholder?: string
  align?: 'start' | 'end'
  menuLabel?: string
}>(), { label: '', disabled: false, placeholder: '请选择', align: 'start', menuLabel: '选项' })

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const items = ref<HTMLButtonElement[]>([])
const activeIndex = ref(-1)

const current = computed(() => props.options.find((item) => item.value === props.modelValue) ?? null)

function openMenu(startAt: 'first' | 'selected' = 'selected'): void {
  if (props.disabled) return
  open.value = true
  const index = startAt === 'selected'
    ? props.options.findIndex((item) => item.value === props.modelValue)
    : 0
  activeIndex.value = index >= 0 ? index : 0
  void nextTick(() => items.value[activeIndex.value]?.focus())
}

function closeMenu(focusTrigger = true): void {
  if (!open.value) return
  open.value = false
  activeIndex.value = -1
  if (focusTrigger) void nextTick(() => trigger.value?.focus())
}

function toggle(): void {
  if (open.value) closeMenu()
  else openMenu()
}

function choose(value: string): void {
  emit('update:modelValue', value)
  closeMenu()
}

function focusItem(index: number): void {
  if (!props.options.length) return
  const next = (index + props.options.length) % props.options.length
  activeIndex.value = next
  items.value[next]?.focus()
}

function onTriggerKeydown(event: KeyboardEvent): void {
  if (props.disabled) return
  switch (event.key) {
    case 'ArrowDown': case 'ArrowUp': case 'Enter': case ' ':
      event.preventDefault()
      openMenu(event.key === 'ArrowUp' ? 'first' : 'selected')
      break
    case 'Escape':
      closeMenu(false)
      break
    default:
  }
}

function onMenuKeydown(event: KeyboardEvent): void {
  switch (event.key) {
    case 'ArrowDown': event.preventDefault(); focusItem(activeIndex.value + 1); break
    case 'ArrowUp': event.preventDefault(); focusItem(activeIndex.value - 1); break
    case 'Home': event.preventDefault(); focusItem(0); break
    case 'End': event.preventDefault(); focusItem(props.options.length - 1); break
    case 'Enter': case ' ':
      event.preventDefault()
      if (props.options[activeIndex.value]) choose(props.options[activeIndex.value]!.value)
      break
    case 'Escape': event.preventDefault(); closeMenu(); break
    case 'Tab':
      closeMenu(false)
      break
    default:
  }
}

/** 点击外部自动关闭。 */
function onPointerDown(event: PointerEvent): void {
  if (!open.value) return
  if (root.value && !root.value.contains(event.target as Node)) closeMenu(false)
}
watch(open, (isOpen) => {
  if (isOpen) document.addEventListener('pointerdown', onPointerDown, true)
  else document.removeEventListener('pointerdown', onPointerDown, true)
})
watch(() => props.disabled, (disabled) => { if (disabled) closeMenu(false) })
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown, true))

function setItemRef(element: unknown, index: number): void {
  if (element instanceof HTMLButtonElement) items.value[index] = element
}
</script>

<template>
  <div ref="root" class="ui-dropdown" :class="{ open, disabled, [`align-${align}`]: true }">
    <span v-if="label" class="ui-dropdown-label">{{ label }}</span>
    <button
      ref="trigger"
      type="button"
      class="ui-dropdown-trigger"
      :disabled="disabled"
      :aria-expanded="open"
      aria-haspopup="listbox"
      :aria-label="label ? `${label}：${current?.label ?? placeholder}` : (current?.label ?? placeholder)"
      @click="toggle"
      @keydown="onTriggerKeydown"
    >
      <span class="ui-dropdown-value">{{ current?.label ?? placeholder }}</span>
      <ChevronDown :size="15" class="ui-dropdown-caret" :class="{ flipped: open }" aria-hidden="true" />
    </button>

    <Transition name="ui-dropdown-pop">
      <div v-if="open" class="ui-dropdown-menu" role="listbox" :aria-label="menuLabel" :aria-activedescendant="undefined" @keydown="onMenuKeydown">
        <button
          v-for="(option, index) in options"
          :key="option.value"
          :ref="(element) => setItemRef(element, index)"
          type="button"
          role="option"
          class="ui-dropdown-option"
          :class="{ selected: option.value === modelValue, focused: index === activeIndex }"
          :aria-selected="option.value === modelValue"
          :tabindex="index === activeIndex ? 0 : -1"
          @click="choose(option.value)"
          @focus="activeIndex = index"
        >
          <span class="ui-option-text"><strong>{{ option.label }}</strong><small v-if="option.hint">{{ option.hint }}</small></span>
          <Check v-if="option.value === modelValue" :size="14" class="ui-option-check" aria-hidden="true" />
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.ui-dropdown { position: relative; display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.ui-dropdown-label { color: var(--text-secondary); font-size: 12px; white-space: nowrap; }
.ui-dropdown-trigger {
  display: inline-flex; align-items: center; gap: 8px;
  min-width: 148px; min-height: 32px; padding: 0 10px 0 12px;
  border: 1px solid #E5E7EB; border-radius: 12px;
  background: var(--surface); color: var(--text-primary);
  font: inherit; font-size: 12.5px; line-height: 1; text-align: left;
  cursor: pointer; transition: border-color .14s ease, background .14s ease, box-shadow .14s ease;
}
.ui-dropdown-trigger:hover:not(:disabled) { border-color: #D6DAE0; background: #FCFCFD; }
.ui-dropdown-trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ui-dropdown-trigger:disabled { opacity: .55; cursor: not-allowed; }
.ui-dropdown.open .ui-dropdown-trigger { border-color: #D6DAE0; background: #FCFCFD; }
.ui-dropdown-value { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ui-dropdown-caret { flex-shrink: 0; color: var(--text-muted); transition: transform .16s ease; }
.ui-dropdown-caret.flipped { transform: rotate(180deg); }

.ui-dropdown-menu {
  position: absolute; z-index: 70; top: calc(100% + 5px); left: 0;
  display: grid; gap: 2px;
  min-width: 100%; width: max-content; max-width: 280px;
  padding: 6px;
  border: 1px solid #E5E7EB; border-radius: 12px;
  background: var(--surface);
  box-shadow: 0 8px 24px rgb(0 0 0 / .08);
}
.ui-dropdown.align-end .ui-dropdown-menu { left: auto; right: 0; }
.ui-dropdown-option {
  display: flex; align-items: center; gap: 10px; width: 100%;
  min-height: 34px; padding: 6px 9px;
  border: 0; border-radius: 8px; background: transparent;
  color: var(--text-primary); font: inherit; font-size: 12.5px; text-align: left; cursor: pointer;
  transition: background .12s ease, color .12s ease;
}
.ui-dropdown-option:hover, .ui-dropdown-option.focused { background: #F3F4F6; }
.ui-dropdown-option:focus-visible { outline: 2px solid var(--accent); outline-offset: -1px; }
/* 选中态：低饱和浅蓝底 + 蓝字 + ✓，不用整块深色矩形 */
.ui-dropdown-option.selected { background: #EFF4FC; color: #2F5D9E; }
.ui-dropdown-option.selected:hover { background: #E8F0FA; }
.ui-option-text { display: grid; gap: 1px; flex: 1; min-width: 0; }
.ui-option-text strong { font-size: 12.5px; font-weight: 500; }
.ui-option-text small { color: var(--text-muted); font-size: 10.5px; }
.ui-dropdown-option.selected .ui-option-text small { color: #5C7FB0; }
.ui-option-check { flex-shrink: 0; color: #2F5D9E; }

/* 120–180ms 轻微淡入 + 上浮 */
.ui-dropdown-pop-enter-active, .ui-dropdown-pop-leave-active { transition: opacity .15s ease, transform .15s ease; }
.ui-dropdown-pop-enter-from, .ui-dropdown-pop-leave-to { opacity: 0; transform: translateY(-4px); }
@media (prefers-reduced-motion: reduce) {
  .ui-dropdown-pop-enter-active, .ui-dropdown-pop-leave-active, .ui-dropdown-caret { transition: none; }
}
</style>
