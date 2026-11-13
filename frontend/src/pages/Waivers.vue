<script setup>
import { computed, onMounted, ref } from 'vue'
import { delJSON, getJSON, postJSON } from '../api'

const datasets = ref([])
const rules = ref([])
const waivers = ref([])
const err = ref('')

const datasetId = ref(1)
const ruleId = ref(null)
const entityKey = ref('')
const startsAt = ref('')
const endsAt = ref('')
const note = ref('')
const submitting = ref(false)

const rulesInDataset = computed(() => rules.value.filter((r) => r.dataset_id === Number(datasetId.value)))
const datasetName = (id) => datasets.value.find((d) => d.id === id)?.name ?? id
const ruleName = (id) => rules.value.find((r) => r.id === id)?.name ?? id

function localInputValue(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function toIso(localValue) {
  // datetime-local 无时区，按浏览器本地时间解析后转 UTC ISO 串
  return new Date(localValue).toISOString()
}

function fillShortWindow(seconds = 90) {
  const now = new Date()
  startsAt.value = localInputValue(now)
  endsAt.value = localInputValue(new Date(now.getTime() + seconds * 1000))
}

async function loadWaivers() {
  const params = datasetId.value ? `?dataset_id=${datasetId.value}` : ''
  waivers.value = await getJSON(`/api/waivers${params}`)
}

async function onDatasetChange() {
  ruleId.value = rulesInDataset.value[0]?.id ?? null
  await loadWaivers()
}

async function submit() {
  err.value = ''
  if (!ruleId.value) {
    err.value = '请选择规则'
    return
  }
  if (!startsAt.value || !endsAt.value) {
    err.value = '请填写起止时间'
    return
  }
  submitting.value = true
  try {
    await postJSON('/api/waivers', {
      rule_id: Number(ruleId.value),
      entity_key: entityKey.value.trim() || null,
      starts_at: toIso(startsAt.value),
      ends_at: toIso(endsAt.value),
      note: note.value.trim() || null,
    })
    entityKey.value = ''
    note.value = ''
    await loadWaivers()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    submitting.value = false
  }
}

async function remove(w) {
  err.value = ''
  try {
    await delJSON(`/api/waivers/${w.id}`)
    await loadWaivers()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(async () => {
  try {
    datasets.value = await getJSON('/api/datasets')
    rules.value = await getJSON('/api/rules')
    if (datasets.value.length) {
      datasetId.value = datasets.value[0].id
      ruleId.value = rulesInDataset.value[0]?.id ?? null
    }
    await loadWaivers()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>
<template>
  <div class="page">
    <header>
      <h1>规则限时豁免</h1>
      <p class="muted">
        活动期临时压过规则命中，规则本身保持启用；仅有效期内生效，过期自动恢复拦截。
        实体键留空 = 该规则下全部实体；填写后只豁免点名的键。
      </p>
    </header>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel">
      <h3>登记豁免</h3>
      <div class="row">
        <label>数据集
          <select v-model="datasetId" @change="onDatasetChange">
            <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.id }} · {{ d.name }}</option>
          </select>
        </label>
        <label>规则
          <select v-model="ruleId">
            <option v-for="r in rulesInDataset" :key="r.id" :value="r.id">
              {{ r.id }} · {{ r.name }}（{{ r.severity }}{{ r.enabled ? '' : '·已停用' }}）
            </option>
          </select>
        </label>
        <label>实体键（可选）
          <input v-model="entityKey" placeholder="留空=全部实体，如 ORD-3" style="width:220px" />
        </label>
      </div>
      <div class="row" style="margin-top:10px">
        <label>生效起
          <input v-model="startsAt" type="datetime-local" />
        </label>
        <label>生效止（不含该时刻）
          <input v-model="endsAt" type="datetime-local" />
        </label>
        <button type="button" @click="fillShortWindow(90)">填 90 秒短窗</button>
      </div>
      <div class="row" style="margin-top:10px">
        <label>备注
          <input v-model="note" placeholder="豁免事由" style="width:260px" />
        </label>
        <button :disabled="submitting" @click="submit">{{ submitting ? '提交中…' : '登记豁免' }}</button>
      </div>
    </div>

    <div class="panel">
      <h3>豁免清单 <span class="muted">（数据集 #{{ datasetId }}）</span></h3>
      <table>
        <thead>
          <tr><th>ID</th><th>规则</th><th>实体键</th><th>生效起</th><th>生效止</th><th>状态</th><th>备注</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="w in waivers" :key="w.id">
            <td>{{ w.id }}</td>
            <td>{{ w.rule_id }} · {{ w.rule_name }}<br /><span class="tag" :class="w.severity === 'error' ? 'danger' : ''">{{ w.severity }}</span></td>
            <td><code v-if="w.entity_key">{{ w.entity_key }}</code><span v-else class="tag">全部实体</span></td>
            <td>{{ w.starts_at }}</td>
            <td>{{ w.ends_at }}</td>
            <td><span class="tag" :class="w.active ? 'ok' : ''">{{ w.active ? '活动中' : '未生效/已过期' }}</span></td>
            <td class="muted">{{ w.note || '—' }}</td>
            <td><button @click="remove(w)">删除</button></td>
          </tr>
          <tr v-if="!waivers.length">
            <td colspan="8" class="muted">暂无豁免单</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
