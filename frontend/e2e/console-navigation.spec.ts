import { expect, test } from 'playwright/test'

const service = {
  id: '42',
  name: 'nous-qwen21-text-to-image',
  status: 'active',
  category: 'image',
  source_type: 'comfy_template',
  source_id: '17',
  workflow_id: null,
  workflow_snapshot: { nodes: [] },
  exposed_inputs: [],
  exposed_outputs: [],
  models: [],
}

test('retired workflow URLs return to services and ComfyUI editing stays available', async ({ page }) => {
  await page.route('**/sys/admin/me', route => route.fulfill({
    json: { login_required: false, authenticated: true },
  }))
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    switch (path) {
      case '/api/v1/services':
        return route.fulfill({ json: [service] })
      case '/api/v1/services/42':
        return route.fulfill({ json: service })
      case '/api/v1/comfy-templates/17':
        return route.fulfill({
          json: { id: '17', name: service.name, workflow_json: {}, exposed_params: [] },
        })
      case '/api/v1/comfy/health':
        return route.fulfill({ json: { reachable: true, devices: [] } })
      case '/api/v1/comfy/object-info':
        return route.fulfill({ json: {} })
      case '/api/v1/tasks/gallery':
        return route.fulfill({ json: { items: [], next_before: null } })
      default:
        return route.fulfill({ json: [] })
    }
  })

  for (const path of ['/workflows', '/workflows/55?publish=1']) {
    await page.goto(path)
    await expect(page).toHaveURL(/\/services$/)
    await expect(page.getByRole('heading', { name: '服务', exact: true })).toBeVisible()
    await expect(page.getByRole('navigation', { name: '主导航' })
      .getByRole('button', { name: '工作流', exact: true })).toHaveCount(0)
  }

  await page.getByRole('button', { name: '新建服务' }).click()
  await expect(page.getByRole('menuitem', { name: /导入 ComfyUI 工作流/ })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: /从 Workflow 发布/ })).toHaveCount(0)

  await page.goto('/services/42')
  await expect(page.getByRole('heading', { name: service.name })).toBeVisible()
  await page.getByRole('button', { name: '编辑暴露字段' }).click()
  await expect(page.getByText('工作流参数映射')).toBeVisible()

  await page.getByRole('button', { name: '图库', exact: true }).click()
  await expect(page.getByRole('heading', { name: '图库', exact: true })).toBeVisible()
})
