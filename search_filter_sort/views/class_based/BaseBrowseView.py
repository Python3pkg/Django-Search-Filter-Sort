import operator
import logging
from functools import reduce

from django.db.models import Q
from django.http.response import HttpResponseRedirect
from django.views.generic import ListView
from django.conf import settings
from django.core.exceptions import FieldError

from search_filter_sort.utils.misc import class_strings_to_class, convert_age_to_date

logger = logging.getLogger(__name__)
USER_SEARCH_LIST_DEFAULT = ["username", "first_name", "last_name", "email"]

if hasattr(settings, "USER_SEARCH_LIST"):
    USER_SEARCH_LIST = settings.USER_SEARCH_LIST
else:
    USER_SEARCH_LIST = USER_SEARCH_LIST_DEFAULT


class BaseBrowseView(ListView):
    template_name = None
    model = None
    should_override_pagination = False
    searches = []
    filters = []
    filter_names = []
    sorts = []
    default_sort_by = []
    default_pagination = 25

    search_by = None
    using_filters = None

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(BaseBrowseView, self).dispatch(request, *args, **kwargs)
        except (ValueError, TypeError, FieldError) as e:
            # Related Field got invalid lookup: xxxx
            # This happens if get_queryset returns queryset that can't be evaluated by ListView
            # Implemented to make OpenVAS scanner happy (it thinks it is buffer overflow error, ha)

            # Call custom error handler (it can log additional info or call set_notification)
            self.get_queryset_error_handler()
            logger.error("BaseBrowseView: Incorrect filter name or value (%s)" % request.get_full_path())
            logger.error("BaseBrowseView: Exception %s" % e)

            # To avoid infinite redirection loop
            if request.path == request.get_full_path():
                raise
            return HttpResponseRedirect(request.path)

    def get_context_data(self, **kwargs):
        context = super(BaseBrowseView, self).get_context_data(**kwargs)
        # check_search_fields()

        context["paginate_by"] = self.paginate_by
        context["search_by"] = self.search_by
        context["filters"] = self.filters
        context["filter_names"] = self.filter_names
        context["using_filters"] = self.using_filters
        context["default_pagination"] = self.default_pagination

        return context

    def get_queryset_error_handler(self):
        # Called if filter provide in query string is incorrect. Can be modified by child classes.
        pass

    def get_queryset(self):
        self.searches = self.search_fields(self.model, [])

        if not self.should_override_pagination:
            try:
                self.paginate_by = int(self.request.GET.get("paginate_by", self.default_pagination))
            except:
                self.paginate_by = self.default_pagination

        should_return_empty = self.request.GET.get("__RETURN_EMPTY__", None)

        if should_return_empty:
            return self.model.objects.none()

        search_bys = self.request.GET.get("search_by", None)
        filter_names = self.request.GET.getlist("filter_name", None)
        filter_values = self.request.GET.getlist("filter_value", None)
        sort_bys = self.request.GET.getlist("sort_by", self.default_sort_by)

        search_list = self.get_search_list(search_bys)
        filter_list = self.get_filter_list(filter_names, filter_values)
        sort_list = self.get_sort_list(sort_bys)

        # Search, filter, sort
        if search_list:
            list_of_search_bys_Q = [Q(**{key: value}) for key, value in list(search_list.items())]
            search_reduce = reduce(operator.or_, list_of_search_bys_Q)
        else:
            search_reduce = None

        if filter_list:
            list_of_filter_bys_Q = [[Q(**{key: value}) for value in array] for key, array in list(filter_list.items())]
            reduced_filters = []

            for array in list_of_filter_bys_Q:
                reduced_filters.append(reduce(operator.or_, array))

            filter_reduce = reduce(operator.and_, reduced_filters)
            self.using_filters = True
        else:
            filter_reduce = None
            self.using_filters = False

        if search_reduce and filter_reduce:
            queryset = self.model.objects.filter(search_reduce).filter(filter_reduce).distinct().order_by(*sort_list)
        elif search_reduce:
            queryset = self.model.objects.filter(search_reduce).distinct().order_by(*sort_list)
        elif filter_reduce:
            queryset = self.model.objects.filter(filter_reduce).distinct().order_by(*sort_list)
        else:
            queryset = self.model.objects.order_by(*sort_list)

        return queryset

    def get_search_list(self, search_bys):
        # Determine search_list
        search_list = {}

        if search_bys:
            self.search_by = search_bys
            search_terms = []

            for term in search_bys.split():
                search_terms.append(term)

            for field in self.searches:
                field += "__icontains"

                for term in search_terms:
                    search_list[field] = term
        else:
            self.search_by = ""

        return search_list

    def get_filter_list(self, filter_names, filter_values):
        # Determine filter_list
        filter_list = {}
        self.define_filters()

        for i in range(len(filter_names)):
            filter_name = filter_names[i]

            # This is only false if there are more filter_names than filter_values. Should be equal.
            if i < len(filter_values):
                values = filter_values[i].split(",")

                if "__lte_age" in filter_name or "__lt_age" in filter_name:
                    values = [convert_age_to_date(int(filter_values[i]))]
                    filter_name = filter_name.replace("__lte_age", "__lte")
                    filter_name = filter_name.replace("__lt_age", "__lt")
                elif "__lte_number" in filter_name or "__lt_number" in filter_name:
                    filter_name = filter_name.replace("__lte_number", "__lte")
                    filter_name = filter_name.replace("__lt_number", "__lt")

                if "__gte_age" in filter_name or "__gt_age" in filter_name:
                    values = [convert_age_to_date(int(filter_values[i]) + 1)]
                    filter_name = filter_name.replace("__gte_age", "__gte")
                    filter_name = filter_name.replace("__gt_age", "__gt")
                elif "__gte_number" in filter_name or "__gt_number" in filter_name:
                    filter_name = filter_name.replace("__gte_number", "__gte")
                    filter_name = filter_name.replace("__gt_number", "__gt")

                new_values = []
                for value in values:
                    if value == "__NONE_OR_BLANK__":
                        new_values.append("")
                        value = None
                    elif value == "__NONE__":
                        value = None
                    elif value == "__BLANK__":
                        value = ""
                    elif value == "__TRUE__":
                        value = True
                    elif value == "__FALSE__":
                        value = False

                    new_values.append(value)

                values = new_values
                filter_list[filter_name] = values
            else:
                break

        return filter_list

    def get_sort_list(self, sort_bys):
        # Determine sort_list
        sort_list = list(sort_bys)
        count = 0

        for i in range(len(sort_bys)):
            if "-" in sort_bys[i]:
                base_sort = sort_bys[i].split("-")[1]
            else:
                base_sort = sort_bys[i]

            if base_sort not in self.sorts:
                sort_list.remove(sort_bys[i])
                logger.debug("Sort of " + base_sort + " is not in the sorts.")
                count -= 1
            elif "last_name" in sort_bys[i]:  # Special clause for last_names/first_names
                sort_list.insert(count, sort_bys[i].replace("last_name", "first_name"))
                count += 1
            elif base_sort == "birthday":  # Special clause for birthday/age. Need to reverse order because it is backwards for some reason.
                if sort_bys[i] == "birthday":
                    sort_list[count] = "-birthday"
                else:
                    sort_list[count] = "birthday"
            count += 1

        return sort_list

    def define_filters(self):
        self.filters = []
        self.filter_names = []

    def add_select_filter(self, html_name, filter_name, html_options_code):
        html_code = '<select class="multi-select form-control" id="' + filter_name + '-filter" name="' + filter_name + '_filter" autocomplete="off" multiple>'
        html_code += html_options_code + '</select>'

        self.filters.append(
        {
            "filter_name": filter_name,
            "html_name": html_name,
            "html_code": html_code
        })

        self.filter_names.append(filter_name)

    def add_number_range_filter(self, html_name, lower_filter_name, upper_filter_name, max_width="50px", step_size="1"):
        html_code = \
            '<input type="number" class="range-filter form-control" id="' + lower_filter_name + '-filter" ' + \
                'name="' + lower_filter_name + '" step="' + step_size + '" style="max-width: ' + max_width + '" />' + \
            '<b> - </b>' + \
            '<input type="number" class="range-filter form-control" id="' + upper_filter_name + '-filter" ' + \
                'name="' + upper_filter_name + '" step="' + step_size + '" style="max-width: ' + max_width + '" />'

        self.filters.append(
        {
            "html_name": html_name,
            "html_code": html_code
        })

        self.filter_names.append(lower_filter_name)
        self.filter_names.append(upper_filter_name)

    def search_fields(self, class_object, list_of_used_classes):
        object_search_list = []

        if class_object in list_of_used_classes:
            return []
        else:
            list_of_used_classes.append(class_object)

        if class_object.__name__ == "User":
            search_list = [search_item for search_item in USER_SEARCH_LIST]
        else:
            object_dependencies = class_object.object_dependencies()

            for object_dependency in object_dependencies:
                if object_dependency[2] == "User":
                    object_search_list += [
                        str(object_dependency[0] + "__{0}").format(search_item) for search_item in USER_SEARCH_LIST
                    ]
                else:
                    other_class_object = class_strings_to_class(object_dependency[1], object_dependency[2])
                    other_object_search_list = self.search_fields(other_class_object, list_of_used_classes)
                    object_search_list += [str(object_dependency[0] + "__{0}").format(search_item) for search_item in
                                           other_object_search_list]

            search_list = class_object.basic_search_list() + class_object.special_search_list() + object_search_list

        return search_list
