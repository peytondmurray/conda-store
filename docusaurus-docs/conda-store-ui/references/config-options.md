---
description: Configuration options for conda-store-ui
---

# Configuration options

The possible options are stored in a key-value format.

| Key                | Value  |
|--------------------|--------|
|`REACT_APP_API_URL` | string |

Base API url that will be used when creating RTK Query queries

| Key                    | Value  |
|------------------------|--------|
|`REACT_APP_AUTH_METHOD` | string |

Preferred method of authentication.

Options:

* `cookie`

Lets users authenticate by logging into `conda-store`. This is the preferred method of authentication.

* `token`

Lets users utilize a token generated by `conda-store` to authenticate to the server.

| Key                       | Value  |
|---------------------------|--------|
|`REACT_APP_LOGIN_PAGE_URL` | string |

The URL endpoint used for login authentication.

| Key                        | Value  |
|----------------------------|--------|
|`REACT_APP_LOGOUT_PAGE_URL` | string |

The URL endpoint used for logout authentication.

| Key                    | Value              |
|------------------------|--------------------|
|`REACT_APP_AUTH_TOKEN`  | `null` / `string`  |

If using the `token` method for authentication, then input the token as a string.
Otherwise, leave blank.

| Key                       | Value  |
|---------------------------|--------|
|`REACT_APP_STYLE_TYPE`     | string |

Options:

* `grayscale`

* `green-accent`

| Key                        | Value  |
|----------------------------|--------|
|`REACT_APP_SHOW_AUTH_BUTTON`|  bool  |

Options:

* `true`

Show the login icon in the top menubar.

* `false`

Hide the login icon in the top menubar.