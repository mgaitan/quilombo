import re

from django import forms
from django.utils.translation import gettext_lazy as _

from .attribute_profiles import normalize_item_attributes, schema_item_defaults
from .catalogs import normalize_edition_key, normalize_isbn
from .models import Holding, Item, Location
from .services import normalize_aliases


class LocationChoiceField(forms.ModelChoiceField):
    def __init__(self, *args, labels=None, **kwargs):
        self.labels = labels or {}
        super().__init__(*args, **kwargs)

    def label_from_instance(self, location):
        return self.labels.get(location.id, location.name)


def _location_choices(workspace, *, exclude_id=None):
    locations = list(workspace.locations.only("id", "parent_id", "name"))
    by_id = {location.id: location for location in locations}
    labels = {}
    for location in locations:
        current = location
        path = []
        seen = set()
        while current and current.id not in seen:
            seen.add(current.id)
            path.append(current.name)
            current = by_id.get(current.parent_id)
        labels[location.id] = " → ".join(reversed(path))
    excluded_ids = {exclude_id} if exclude_id else set()
    if exclude_id:
        for location in locations:
            current = location
            seen = set()
            while current and current.id not in seen:
                seen.add(current.id)
                if current.parent_id == exclude_id:
                    excluded_ids.add(location.id)
                    break
                current = by_id.get(current.parent_id)
    return workspace.locations.exclude(id__in=excluded_ids).order_by("name", "id"), labels


class AliasesFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.initial["aliases"] = ", ".join(self.instance.aliases)

    def clean_aliases(self):
        return normalize_aliases((self.cleaned_data.get("aliases") or "").split(","))


class ItemForm(AliasesFormMixin, forms.ModelForm):
    schema = forms.ChoiceField(
        label=_("Type"),
        choices=[
            ("", _("Generic object")),
            ("book", _("Book")),
        ],
        required=False,
    )
    aliases = forms.CharField(
        label=_("Aliases"), required=False, help_text=_("Separate aliases with commas.")
    )
    authors = forms.CharField(label=_("Author(s)"), required=False)
    publishers = forms.CharField(label=_("Publisher(s)"), required=False)
    isbn = forms.CharField(
        label=_("ISBN"),
        required=False,
        help_text=_("Separate multiple ISBNs with commas."),
    )
    openlibrary_edition = forms.CharField(label=_("Open Library edition"), required=False)
    publication_date = forms.CharField(label=_("Publication date"), required=False)
    publication_year = forms.IntegerField(label=_("Publication year"), required=False, min_value=0)
    edition = forms.CharField(label=_("Edition"), required=False)
    language = forms.CharField(label=_("Language"), required=False)
    page_count = forms.IntegerField(label=_("Pages"), required=False, min_value=0)

    class Meta:
        model = Item
        fields = [
            "key",
            "name",
            "schema",
            "description",
            "category",
            "aliases",
            "tracking_mode",
            "unit",
            "minimum_quantity",
            "target_quantity",
        ]
        labels = {
            "key": _("Key"),
            "name": _("Name"),
            "description": _("Description"),
            "category": _("Category"),
            "tracking_mode": _("Tracking mode"),
            "unit": _("Unit"),
            "minimum_quantity": _("Minimum quantity"),
            "target_quantity": _("Target quantity"),
        }

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.instance.workspace = workspace
        attributes = self.instance.attributes if isinstance(self.instance.attributes, dict) else {}
        self.initial["schema"] = attributes.get("schema", "")
        defaults = schema_item_defaults(attributes)
        if defaults:
            self.initial.update(defaults)
        book = attributes.get("book") if isinstance(attributes.get("book"), dict) else {}
        identifiers = (
            attributes.get("identifiers") if isinstance(attributes.get("identifiers"), dict) else {}
        )
        for field in ("authors", "publishers"):
            values = book.get(field, [])
            if isinstance(values, list):
                self.initial[field] = ", ".join(str(value) for value in values)
        for field in ("publication_date", "publication_year", "edition", "language", "page_count"):
            if field in book:
                self.initial[field] = book[field]
        isbn_values = identifiers.get("isbn", [])
        if isinstance(isbn_values, str):
            isbn_values = [isbn_values]
        if isinstance(isbn_values, list) and isbn_values:
            self.initial["isbn"] = ", ".join(isbn_values)
        edition_values = identifiers.get("openlibrary_edition", [])
        if isinstance(edition_values, str):
            edition_values = [edition_values]
        if isinstance(edition_values, list) and edition_values:
            self.initial["openlibrary_edition"] = edition_values[0]
        if self.initial["schema"] == "book" or self.data.get("schema") == "book":
            self.fields["tracking_mode"].required = False
            self.fields["unit"].required = False

    def clean_isbn(self):
        value = self.cleaned_data.get("isbn", "")
        values = [part.strip() for part in re.split(r"[,\n]+", value) if part.strip()]
        try:
            return list(dict.fromkeys(normalize_isbn(part) for part in values))
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error

    def clean_openlibrary_edition(self):
        value = self.cleaned_data.get("openlibrary_edition", "").strip()
        if not value:
            return ""
        try:
            return normalize_edition_key(value)
        except ValueError as error:
            raise forms.ValidationError(str(error)) from error

    def clean_key(self):
        key = self.cleaned_data["key"]
        duplicate = Item.objects.filter(workspace=self.workspace, key=key).exclude(
            pk=self.instance.pk
        )
        if duplicate.exists():
            raise forms.ValidationError(_("An item with this key already exists."))
        return key

    def clean(self):
        cleaned_data = super().clean()
        attributes = normalize_item_attributes(
            self.instance.attributes,
            self.instance.category,
        )
        schema = cleaned_data.pop("schema", "")
        if not schema and "schema" not in self.data:
            schema = attributes.get("schema", "")
        if schema:
            attributes["schema"] = schema
        else:
            attributes.pop("schema", None)
        if schema == "book":
            book = attributes.get("book")
            if not isinstance(book, dict):
                book = {}
            for field in ("authors", "publishers"):
                values = normalize_aliases((cleaned_data.get(field) or "").split(","))
                if values:
                    book[field] = values
                else:
                    book.pop(field, None)
            for field in (
                "publication_date",
                "publication_year",
                "edition",
                "language",
                "page_count",
            ):
                value = cleaned_data.get(field)
                if value in (None, ""):
                    book.pop(field, None)
                else:
                    book[field] = value
            if book:
                attributes["book"] = book
            else:
                attributes.pop("book", None)

            identifiers = attributes.get("identifiers")
            if not isinstance(identifiers, dict):
                identifiers = {}
            if cleaned_data.get("isbn"):
                identifiers["isbn"] = cleaned_data["isbn"]
            else:
                identifiers.pop("isbn", None)
            if cleaned_data.get("openlibrary_edition"):
                identifiers["openlibrary_edition"] = [cleaned_data["openlibrary_edition"]]
            else:
                identifiers.pop("openlibrary_edition", None)
            if identifiers:
                attributes["identifiers"] = identifiers
            else:
                attributes.pop("identifiers", None)
        for field in (
            "authors",
            "publishers",
            "isbn",
            "openlibrary_edition",
            "publication_date",
            "publication_year",
            "edition",
            "language",
            "page_count",
        ):
            cleaned_data.pop(field, None)
        cleaned_data["attributes"] = attributes
        cleaned_data.update(schema_item_defaults(attributes))
        if (
            self.instance.pk
            and cleaned_data.get("tracking_mode") == Item.TrackingMode.DISCRETE
            and any(
                quantity != quantity.to_integral_value()
                for quantity in self.instance.holdings.values_list("quantity", flat=True)
            )
        ):
            self.add_error(
                "tracking_mode",
                _("All holdings must have whole quantities before using discrete tracking."),
            )
        return cleaned_data


class LocationForm(AliasesFormMixin, forms.ModelForm):
    aliases = forms.CharField(
        label=_("Aliases"), required=False, help_text=_("Separate aliases with commas.")
    )

    class Meta:
        model = Location
        fields = ["key", "name", "description", "kind", "parent", "aliases"]
        labels = {
            "key": _("Key"),
            "name": _("Name"),
            "description": _("Description"),
            "kind": _("Kind"),
            "parent": _("Inside"),
        }

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.instance.workspace = workspace
        queryset, labels = _location_choices(workspace, exclude_id=self.instance.pk)
        self.fields["parent"] = LocationChoiceField(
            queryset=queryset,
            labels=labels,
            label=_("Inside"),
            required=False,
            empty_label=_("Top level"),
        )
        if self.instance.parent_id:
            self.initial["parent"] = self.instance.parent_id

    def clean_key(self):
        key = self.cleaned_data["key"]
        duplicate = Location.objects.filter(workspace=self.workspace, key=key).exclude(
            pk=self.instance.pk
        )
        if duplicate.exists():
            raise forms.ValidationError(_("A location with this key already exists."))
        return key

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        ancestor = parent
        while ancestor:
            if self.instance.pk and ancestor.pk == self.instance.pk:
                raise forms.ValidationError(_("A location cannot be inside itself."))
            ancestor = ancestor.parent
        return parent


class HoldingForm(forms.ModelForm):
    class Meta:
        model = Holding
        fields = ["location", "quantity", "approximate", "notes"]
        labels = {
            "location": _("Location"),
            "quantity": _("Quantity"),
            "approximate": _("Approximate"),
            "notes": _("Notes"),
        }

    def __init__(self, *args, workspace, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.item = item
        self.instance.workspace = workspace
        if item:
            self.instance.item = item
        locations, labels = _location_choices(workspace)
        if item:
            occupied = item.holdings.exclude(pk=self.instance.pk).values("location_id")
            locations = locations.exclude(id__in=occupied)
        self.fields["location"] = LocationChoiceField(
            queryset=locations,
            labels=labels,
            label=_("Location"),
        )
        if self.instance.location_id:
            self.initial["location"] = self.instance.location_id

    def clean_quantity(self):
        quantity = self.cleaned_data["quantity"]
        item = self.item or self.instance.item
        if (
            item.tracking_mode == Item.TrackingMode.DISCRETE
            and quantity != quantity.to_integral_value()
        ):
            raise forms.ValidationError(_("Discrete items require a whole quantity."))
        return quantity


class WorkspaceCreateForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)


class WorkspaceRenameForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)


class WorkspaceShareForm(forms.Form):
    username = forms.CharField(label=_("Username"), max_length=150)
    can_write = forms.BooleanField(label=_("Can edit"), required=False, initial=True)


class MemberAccessForm(forms.Form):
    can_write = forms.BooleanField(label=_("Can edit"), required=False)
