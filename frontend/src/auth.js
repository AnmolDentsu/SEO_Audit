export async function getCurrentUser() {
  const response = await fetch('/auth/me', {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
    },
  })

  if (!response.ok) {
    throw new Error(
      `Authentication check failed (${response.status})`
    )
  }

  return response.json()
}


export function startLogin() {
  window.location.assign('/auth/login')
}


export async function logout() {
  const response = await fetch('/auth/logout', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
    },
  })

  if (!response.ok) {
    throw new Error(
      `Logout failed (${response.status})`
    )
  }

  window.location.assign('/')
}