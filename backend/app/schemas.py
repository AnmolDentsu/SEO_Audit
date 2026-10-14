from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.services.compliance_guard import (
    require_public_data_confirmation,
    validate_non_sensitive_text,
    validate_public_url_shape,
)


VALID_ROLES = {
    "CLIENT",
    "P0",
    "P1",
    "OTHER",
}

VALID_KEYWORD_TYPES = {
    "PRIMARY",
    "SECONDARY",
    "OTHER",
}


class ProjectCreate(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=200,
    )

    client_name: str | None = Field(
        default=None,
        max_length=200,
    )

    parent_project_id: int | None = Field(
        default=None,
        ge=1,
    )

    @field_validator("name")
    @classmethod
    def clean_project_name(
        cls,
        value: str,
    ):
        value = " ".join(
            value.split()
        )

        if len(value) < 2:
            raise ValueError(
                "project name must contain at least 2 characters"
            )

        return validate_non_sensitive_text(
            value,
            field_name="Project name",
        )

    @field_validator("client_name")
    @classmethod
    def clean_client_name(
        cls,
        value: str | None,
    ):
        if value is None:
            return None

        value = " ".join(
            value.split()
        )

        return validate_non_sensitive_text(
            value,
            field_name="Client/project label",
        )


class ProjectUpdate(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=200,
    )

    client_name: str | None = Field(
        default=None,
        max_length=200,
    )

    parent_project_id: int | None = Field(
        default=None,
        ge=1,
    )

    @field_validator("name")
    @classmethod
    def clean_project_name(
        cls,
        value: str | None,
    ):
        if value is None:
            return None

        value = " ".join(
            value.split()
        )

        if len(value) < 2:
            raise ValueError(
                "project name must contain at least 2 characters"
            )

        return validate_non_sensitive_text(
            value,
            field_name="Project name",
        )

    @field_validator("client_name")
    @classmethod
    def clean_client_name(
        cls,
        value: str | None,
    ):
        if value is None:
            return None

        value = " ".join(
            value.split()
        )

        return validate_non_sensitive_text(
            value,
            field_name="Client/project label",
        )


class TargetCreate(BaseModel):
    url: HttpUrl
    role: str = "OTHER"

    label: str | None = Field(
        default=None,
        max_length=200,
    )

    @field_validator("url")
    @classmethod
    def validate_url(
        cls,
        value: HttpUrl,
    ):
        validate_public_url_shape(
            str(value)
        )
        return value

    @field_validator("role")
    @classmethod
    def validate_role(
        cls,
        value: str,
    ):
        value = (
            value
            .strip()
            .upper()
        )

        if value not in VALID_ROLES:
            raise ValueError(
                f"role must be one of "
                f"{sorted(VALID_ROLES)}"
            )

        return value

    @field_validator("label")
    @classmethod
    def validate_label(
        cls,
        value: str | None,
    ):
        return validate_non_sensitive_text(
            value,
            field_name="URL label",
        )


class KeywordCreate(BaseModel):
    keyword: str = Field(
        min_length=1,
        max_length=500,
    )

    keyword_type: str = "OTHER"

    search_volume: int | None = Field(
        default=None,
        ge=0,
    )

    @field_validator("keyword")
    @classmethod
    def clean_keyword(
        cls,
        value: str,
    ):
        value = " ".join(
            value.split()
        )

        if not value:
            raise ValueError(
                "keyword cannot be blank"
            )

        return validate_non_sensitive_text(
            value,
            field_name="SEO keyword",
        )

    @field_validator("keyword_type")
    @classmethod
    def validate_keyword_type(
        cls,
        value: str,
    ):
        value = (
            value
            or "OTHER"
        ).strip().upper()

        aliases = {
            "PRIMARY KEYWORD": "PRIMARY",
            "SECONDARY KEYWORD": "SECONDARY",
            "": "OTHER",
            "NONE": "OTHER",
        }

        value = aliases.get(
            value,
            value,
        )

        if value not in VALID_KEYWORD_TYPES:
            raise ValueError(
                "keyword_type must be one of "
                f"{sorted(VALID_KEYWORD_TYPES)}"
            )

        return value


class RunCreate(BaseModel):
    use_cache: bool = True
    force_refresh: bool = False
    enable_ai: bool = True

    crawl_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=10,
    )

    # Compliance evidence is persisted in config_json
    # with the run.
    public_data_confirmed: bool = False

    @model_validator(mode="after")
    def confirm_public_data(
        self,
    ):
        require_public_data_confirmation(
            self.public_data_confirmed
        )

        return self


VALID_SITEMAP_ROLES = {
    "CLIENT",
    "COMPETITOR",
}


class SitemapSourceCreate(BaseModel):
    root_url: HttpUrl

    role: str = "COMPETITOR"

    label: str = Field(
        min_length=1,
        max_length=200,
    )

    @field_validator("root_url")
    @classmethod
    def validate_root_url(
        cls,
        value: HttpUrl,
    ):
        validate_public_url_shape(
            str(value)
        )
        return value

    @field_validator("role")
    @classmethod
    def validate_sitemap_role(
        cls,
        value: str,
    ):
        value = (
            value
            .strip()
            .upper()
        )

        if value not in VALID_SITEMAP_ROLES:
            raise ValueError(
                "role must be one of "
                f"{sorted(VALID_SITEMAP_ROLES)}"
            )

        return value

    @field_validator("label")
    @classmethod
    def validate_label(
        cls,
        value: str,
    ):
        value = " ".join(
            value.split()
        )

        result = validate_non_sensitive_text(
            value,
            field_name="Sitemap label",
        )

        if not result:
            raise ValueError(
                "Sitemap label cannot be blank"
            )

        return result


class SitemapRunCreate(BaseModel):
    use_cache: bool = True
    force_refresh: bool = False
    enable_ai: bool = True

    max_urls_per_source: int | None = Field(
        default=None,
        ge=1,
        le=1_000_000,
    )

    resolve_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=100,
    )

    playwright_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=10,
    )

    resolution_scope: str | None = None

    exclude_non_page_urls: bool | None = None

    public_data_confirmed: bool = False

    @field_validator("resolution_scope")
    @classmethod
    def validate_resolution_scope(
        cls,
        value: str | None,
    ):
        if value is None:
            return None

        value = (
            value
            .strip()
            .lower()
        )

        allowed = {
            "strict_host",
            "registrable_domain",
            "sitemap_authoritative",
        }

        if value not in allowed:
            raise ValueError(
                "resolution_scope must be one of "
                f"{sorted(allowed)}"
            )

        return value

    @model_validator(mode="after")
    def confirm_public_data(
        self,
    ):
        require_public_data_confirmation(
            self.public_data_confirmed
        )

        return self
