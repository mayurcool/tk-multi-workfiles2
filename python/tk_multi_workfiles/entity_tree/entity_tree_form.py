# Copyright (c) 2015 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

"""
Implementation of the entity tree widget consisting of a tree view that displays the 
contents of a Shotgun Data Model, a text search and a filter control.
"""
import weakref

import sgtk
from sgtk.platform.qt import QtCore, QtGui

shotgun_model = sgtk.platform.import_framework("tk-framework-shotgunutils", "shotgun_model")
ShotgunEntityModel = shotgun_model.ShotgunEntityModel

from ..ui.entity_tree_form import Ui_EntityTreeForm
from .entity_tree_proxy_model import EntityTreeProxyModel
from ..framework_qtwidgets import Breadcrumb, ShotgunModelOverlayWidget
from ..util import get_model_data, get_model_str

class EntityTreeForm(QtGui.QWidget):
    """
    Entity tree widget class
    """
    class _EntityBreadcrumb(Breadcrumb):
        """
        """
        def __init__(self, label, entity):
            """
            """
            Breadcrumb.__init__(self, label)
            self.entity = entity

    # Signal emitted when an entity is selected in the tree.
    entity_selected = QtCore.Signal(object, object)# selection details, breadcrumbs

    # Signal emitted when the 'New Task' button is clicked.
    create_new_task = QtCore.Signal(object, object)# entity, step

    def __init__(self, entity_model, search_label, allow_task_creation, parent=None):
        """
        Construction

        :param entity_model:    The Shotgun Model this widget should connect to
        :param search_label:    The hint label to be displayed on the search control
        :param parent:          The parent QWidget for this control
        """
        QtGui.QWidget.__init__(self, parent)
        
        # control if step->tasks in the entity hierarchy should be collapsed when building
        # the search details.
        self._collapse_steps_with_tasks = True
        # keep track of the entity to select when the model is updated:
        self._entity_to_select = None
        # keep track of the currently selected item:
        self._current_item_ref = None
        
        # keep track of expanded items as items in the tree are expanded/collapsed.  We
        # also want to auto-expand root items the first time they appear so track them
        # as well
        self._expanded_items = set()
        self._auto_expanded_root_items = set()
        
        # set up the UI
        self._ui = Ui_EntityTreeForm()
        self._ui.setupUi(self)
        
        self._ui.search_ctrl.set_placeholder_text("Search %s" % search_label)
        
        # connect up controls:
        self._ui.search_ctrl.search_edited.connect(self._on_search_changed)

        # enable/hide the my-tasks-only button if we are showing tasks:
        have_tasks = (entity_model and entity_model.get_entity_type() == "Task")
        if have_tasks:
            self._ui.my_tasks_cb.toggled.connect(self._on_my_tasks_only_toggled)
        else:
            self._ui.my_tasks_cb.hide()

        # enable/hide the new task button if we have tasks and task creation is allowed:
        if have_tasks and allow_task_creation:
            # enable and connect the new task button
            self._ui.new_task_btn.clicked.connect(self._on_new_task)
            self._ui.new_task_btn.setEnabled(False)
        else:
            self._ui.new_task_btn.hide()

        self._ui.entity_tree.expanded.connect(self._on_item_expanded)
        self._ui.entity_tree.collapsed.connect(self._on_item_collapsed)
        
        # create the overlay 'busy' widget that will be displayed when the model is reset:
        self._overlay_widget = ShotgunModelOverlayWidget(None, self._ui.entity_tree)
        self._overlay_widget.set_model(entity_model)

        # create a filter proxy model between the source model and the task tree view:
        self._filter_model = EntityTreeProxyModel(["content", {"entity":"name"}], self)
        self._filter_model.rowsInserted.connect(self._on_filter_model_rows_inserted)
        self._filter_model.setSourceModel(entity_model)
        self._ui.entity_tree.setModel(self._filter_model)
        self._expand_root_rows()

        # connect to the selection model for the tree view:
        selection_model = self._ui.entity_tree.selectionModel()
        if selection_model:
            selection_model.selectionChanged.connect(self._on_selection_changed)

    def select_entity(self, entity_type, entity_id):
        """
        Select the specified entity in the tree.  If the tree is still being populated then the selection
        will happen when an item representing the entity appears in the model.

        Note that this doesn't emit an entity_selected signal.

        :param entity_type: The type of the entity to select
        :param entity_id:   The id of the entity to select
        """
        # track the selected entity - this allows the entity to be selected when
        # it appears in the model even if the model hasn't been fully populated yet:
        self._entity_to_select = {"type":entity_type, "id":entity_id}

        # reset the current selection without emitting a signal:
        prev_selected_item = self._reset_selection()
        self._current_item_ref = None
        self._update_ui()

        # try to update the selection to reflect the change:
        self._update_selection(prev_selected_item)

    def get_selection(self):
        """
        Get the currently selected item as well as the breadcrumb trail that represents 
        the path for the selection.

        :returns:   A Tuple containing the details and breadcrumb trail of the current selection:
                        (selection_details, breadcrumb_trail)

                    - selection_details is a dictionary containing:
                      {"label":label, "entity":entity, "children":[children]}
                    - breadcrumb_trail is a list of Breadcrumb instances
        """
        selection_details = {}
        breadcrumb_trail = []

        # get the currently selected index:
        selected_indexes = self._ui.entity_tree.selectionModel().selectedIndexes()
        if len(selected_indexes) == 1:
            selection_details = self._get_entity_details(selected_indexes[0])
            breadcrumb_trail = self._build_breadcrumb_trail()

        return (selection_details, breadcrumb_trail)

    def navigate_to(self, breadcrumb_trail):
        """
        Update the selection to match the specified breadcrumb trail

        :param breadcrumb_trail:    A list of Breadcrumb instances that represent
                                    an item in the tree.
        """
        # figure out the item in the tree to select from the breadcrumb trail:
        src_model = self._filter_model.sourceModel()
        current_item = src_model.invisibleRootItem()
        for crumb in breadcrumb_trail:
            # look for an item under the current item that this breadcrumb represents:
            found_item = None
            if isinstance(crumb, EntityTreeForm._EntityBreadcrumb):
                # look for a child item that represents the entity:
                for row in range(current_item.rowCount()):
                    child_item = current_item.child(row)
                    sg_entity = src_model.get_entity(child_item)
                    if (sg_entity["type"] == crumb.entity["type"]
                        and sg_entity["id"] == crumb.entity["id"]):
                        found_item = child_item
                        break
            else:
                # look for a child item that has the same label:
                for row in range(current_item.rowCount()):
                    child_item = current_item.child(row)
                    if get_model_str(child_item) == crumb.label:
                        found_item = child_item
                        break

            if not found_item:
                # stop traversal!
                break
            else:
                # check to see if the item is visible in the current filtered model:
                filtered_idx = self._filter_model.mapFromSource(found_item.index())
                if not filtered_idx.isValid():
                    # stop traversal!
                    break

            # iterate down to the next level:
            current_item = found_item

        # finally, select the item in the tree:
        idx_to_select = self._filter_model.mapFromSource(current_item.index())
        self._ui.entity_tree.selectionModel().setCurrentIndex(idx_to_select, QtGui.QItemSelectionModel.SelectCurrent)

    # ------------------------------------------------------------------------------------------
    # ------------------------------------------------------------------------------------------

    def _get_selected_item(self):
        """
        Get the currently selected item.

        :returns:   The currently selected model item if any
        """
        item = None
        indexes = self._ui.entity_tree.selectionModel().selectedIndexes()
        if len(indexes) == 1:
            item = self._item_from_index(indexes[0])
        return item

    def _reset_selection(self):
        """
        Reset the current selection, returning the currently selected item if any.  This
        doesn't result in any signals being emitted by the current selection model.

        :returns:   The selected item before the selection was reset if any
        """
        prev_selected_item = self._get_selected_item()
        # reset the current selection without emitting any signals:
        self._ui.entity_tree.selectionModel().reset()
        self._update_ui()
        return prev_selected_item

    def _get_entity_details(self, idx):
        """
        Get entity details for the specified model index.  If steps are being collapsed into tasks
        then these details will reflect that and will not be a 1-1 representation of the tree itself. 

        :param idx: The QModelIndex of the item to get the entity details for. 
        :returns:   A dictionary containing entity information about the specified index containing the 
                    following information:
                    
                        {"label":label, "entity":entity, "children":[children]}

                    - label:      The label of the corresponding item
                    - entity:     The entity dictionary for the corresponding item
                    - children:   A list of immediate children for the corresponding item - each item in 
                                  the list is a dictionary containing 'label' and 'entity'.
        """
        if not idx.isValid():
            return {}

        item = self._item_from_index(idx)
        if not item:
            return {}
        
        # get details for this item:
        label = item.text()
        src_model = self._filter_model.sourceModel()
        entity = src_model.get_entity(item)
        
        # get details for children:
        children = []
        collapsed_children = []
        
        for row in range(self._filter_model.rowCount(idx)):
            child_idx = self._filter_model.index(row, 0, idx)
            child_item = self._item_from_index(child_idx)
            if not child_item:
                continue
            
            child_label = child_item.text()
            child_entity = src_model.get_entity(child_item)
            children.append({"label":child_label, "entity":child_entity})
            
            if self._collapse_steps_with_tasks and child_entity and child_entity["type"] == "Step":
                # see if grand-child is actually a task:
                for child_row in range(self._filter_model.rowCount(child_idx)):
                    grandchild_idx = self._filter_model.index(child_row, 0, child_idx)
                    grandchild_item = self._item_from_index(grandchild_idx)
                    if not grandchild_item:
                        continue
                    
                    grandchild_label = grandchild_item.text()
                    grandchild_entity = src_model.get_entity(grandchild_item)
                    if grandchild_entity and grandchild_entity["type"] == "Task":
                        # found a task under a step so we can safely collapse tasks to steps!
                        collapsed_child_label = "%s - %s" % (child_label, grandchild_label)
                        collapsed_children.append({"label":collapsed_child_label, "entity":grandchild_entity})

        if collapsed_children:
            # prefer collapsed children instead of children if we have them
            children = collapsed_children
        elif self._collapse_steps_with_tasks and entity and entity["type"] == "Step":
            # it's possible that entity is actually a Step and the Children are all tasks - if this is
            # the case then update the child entities to be 'collapsed' and clear the entity on the Step
            # item:
            for child in children:
                child_label = child["label"]
                child_entity = child["entity"]
                if child_entity and child_entity["type"] == "Task":
                    collapsed_child_label = "%s - %s" % (label, child_label)
                    collapsed_children.append({"label":collapsed_child_label, "entity":child_entity})
                    
            if collapsed_children:
                entity = None
                children = collapsed_children
        
        return {"label":label, "entity":entity, "children":children}

    def _on_search_changed(self, search_text):
        """
        Slot triggered when the search text has been changed.

        :param search_text: The new search text
        """
        # reset the current selection without emitting any signals:
        prev_selected_item = self._reset_selection()
        try:
            # update the proxy filter search text:
            self._update_filter(search_text)
        finally:
            # and update the selection - this will restore the original selection if possible.
            self._update_selection(prev_selected_item)

    def _on_my_tasks_only_toggled(self, checked):
        """
        Slot triggered when the show-my-tasks checkbox is toggled

        :param checked: True if the checkbox has been checked, otherwise False 
        """
        # reset the current selection without emitting any signals:
        prev_selected_item = self._reset_selection()
        try:
            self._filter_model.only_show_my_tasks = checked
        finally:
            # and update the selection - this will restore the original selection if possible.
            self._update_selection(prev_selected_item)

    def _update_filter(self, search_text):
        """
        Update the search text in the filter model.

        :param search_text: The new search text to update the filter model with
        """
        filter_reg_exp = QtCore.QRegExp(search_text, QtCore.Qt.CaseInsensitive, QtCore.QRegExp.FixedString)
        self._filter_model.setFilterRegExp(filter_reg_exp)
        
    def _update_selection(self, prev_selected_item):
        """
        Update the selection to either the to-be-selected entity if set or the current item if known.  The 
        current item is the item that was last selected but which may no longer be visible in the view due 
        to filtering.  This allows it to be tracked so that the selection state is correctly restored when 
        it becomes visible again.
        """
        # we want to make sure we don't emit any signals whilst we are 
        # manipulating the selection:
        signals_blocked = self.blockSignals(True)
        try:
            # try to get the item to select:
            item = None
            if self._entity_to_select:
                # we know about an entity we should try to select:
                src_model = self._filter_model.sourceModel()
                if src_model.get_entity_type() == self._entity_to_select["type"]:
                    item = src_model.item_from_entity(self._entity_to_select["type"], self._entity_to_select["id"])
            elif self._current_item_ref:
                # no item to select but we do know about a current item:
                item = self._current_item_ref()

            if item:
                # try to get an index from the current filtered model:
                idx = self._filter_model.mapFromSource(item.index())
                if idx.isValid():
                    # make sure the item is expanded and visible in the tree:
                    self._ui.entity_tree.scrollTo(idx)

                    # select the item:
                    #selection_flags = QtGui.QItemSelectionModel.Clear | QtGui.QItemSelectionModel.SelectCurrent 
                    #self._ui.entity_tree.selectionModel().select(idx, selection_flags)
                    self._ui.entity_tree.selectionModel().setCurrentIndex(idx, QtGui.QItemSelectionModel.SelectCurrent)

        finally:
            self.blockSignals(signals_blocked)

            # if the selection is different to the previously selected item then we
            # will emit an entity_selected signal:
            selected_item = self._get_selected_item()
            if id(selected_item) != id(prev_selected_item):
                # emit a selection changed signal:
                selection_details = None
                if selected_item:
                    selection_details = self._get_entity_details(selected_item.index())

                # emit the signal
                self._emit_entity_selected(selection_details)
                #self.entity_selected.emit(selection_details)

    def _update_ui(self):
        """
        Update the UI to reflect the current selection, etc.
        """
        enable_new_tasks = False

        selected_indexes = self._ui.entity_tree.selectionModel().selectedIndexes()
        if len(selected_indexes) == 1:
            item = self._item_from_index(selected_indexes[0])
            entity = self._filter_model.sourceModel().get_entity(item)
            #if entity and entity.get("type") in ("Step", "Task"):
            if entity and entity["type"] != "Step":
                if entity["type"] == "Task":
                    if entity.get("entity"):
                        enable_new_tasks = True
                else:
                    enable_new_tasks = True

        self._ui.new_task_btn.setEnabled(enable_new_tasks)

    def _on_selection_changed(self, selected, deselected):
        """
        Slot triggered when the selection changes due to user action

        :param selected:    QItemSelection containing any newly selected indexes
        :param deselected:  QItemSelection containing any newly deselected indexes
        """
        # out tree is single-selection so extract the newly selected item from the
        # list of indexes:
        selection_details = {}
        item = None
        selected_indexes = selected.indexes()
        if len(selected_indexes) == 1:
            selection_details = self._get_entity_details(selected_indexes[0])
            item = self._item_from_index(selected_indexes[0])

        # update the UI
        self._update_ui()

        # keep track of the current item:
        self._current_item_ref = weakref.ref(item) if item else None

        if self._current_item_ref:
            # clear the entity-to-select as the current item now takes precedence
            self._entity_to_select = None

        # emit selection_changed signal:
        self._emit_entity_selected(selection_details)
        #self.entity_selected.emit(selection_details)

    def _on_filter_model_rows_inserted(self, parent_idx, first, last):
        """
        Slot triggered when new rows are inserted into the filter model.  This allows us
        to reset any expanded state in the tree and reinstate the selection if it wasn't
        changed manually by the user.

        :param parent_idx:  The parent model index of the rows that were inserted
        :param first:       The first row id inserted
        :param last:        The last row id inserted
        """
        # disable widget paint updates whilst we update the expanded state of the tree:
        self._ui.entity_tree.setUpdatesEnabled(False)
        try:
            if parent_idx and parent_idx.isValid():
                # update the expanded state of the parent item:
                item = self._item_from_index(parent_idx)
                if item and weakref.ref(item) in self._expanded_items:
                    self._ui.entity_tree.expand(parent_idx)

            # step through all new rows updating expanded state:
            for row in range(first, last+1):
                idx = self._filter_model.index(row, 0, parent_idx)
                # recursively step through all children of the new row:
                self._fix_expanded_state_r(idx)
        finally:
            # re-enable updates to allow painting to continue
            self._ui.entity_tree.setUpdatesEnabled(True)

    def _fix_expanded_state_r(self, idx):
        """
        Recursively update the expanded state of items in the tree starting from
        the specified index

        :param idx: The index to recursively fix the expanded state of items in the
                    tree
        """
        # update the expanded state of this item:
        item = self._item_from_index(idx)
        if item:
            ref = weakref.ref(item)
            if not idx.parent().isValid():
                # this is a root item
                if ref not in self._auto_expanded_root_items:
                    self._auto_expanded_root_items.add(ref)
                    self._expanded_items.add(ref)

            if ref in self._expanded_items:
                self._ui.entity_tree.expand(idx)

        # iterate through all children:
        for row in range(0, self._filter_model.rowCount(idx)):
            child_idx = idx.child(row, 0)
            self._fix_expanded_state_r(child_idx)

    def _expand_root_rows(self):
        """
        Expand all root rows in the Tree if they have never been expanded
        """
        # disable widget paint updates whilst we update the expanded state of the tree:
        self._ui.entity_tree.setUpdatesEnabled(False)
        try:
            for row in range(self._filter_model.rowCount()):
                idx = self._filter_model.index(row, 0)
                item = self._item_from_index(idx)
                if not item:
                    continue

                ref = weakref.ref(item)
                if ref in self._auto_expanded_root_items:
                    continue

                self._ui.entity_tree.expand(idx)
                self._auto_expanded_root_items.add(ref)
        finally:
            # re-enable updates to allow painting to continue
            self._ui.entity_tree.setUpdatesEnabled(True)

    def _item_from_index(self, idx):
        """
        Find the corresponding model item from the specified index.  This handles
        the indirection introduced by the filter model.

        :param idx: The model index to find the item for
        :returns:   The item in the model represented by the index
        """
        src_idx = self._filter_model.mapToSource(idx)
        return self._filter_model.sourceModel().itemFromIndex(src_idx)

    def _on_item_expanded(self, idx):
        """
        Slot triggered when an item in the tree is expanded - used to track expanded
        state for all items.

        :param idx: The index of the item in the tree being expanded
        """
        item = self._item_from_index(idx)
        if not item:
            return
        self._expanded_items.add(weakref.ref(item))

    def _on_item_collapsed(self, idx):
        """
        Slot triggered when an item in the tree is collapsed - used to track expanded
        state for all items.

        :param idx: The index of the item in the tree being collapsed
        """
        item = self._item_from_index(idx)
        if not item:
            return
        self._expanded_items.discard(weakref.ref(item))

    def _on_new_task(self):
        """
        Slot triggered when the new task button is clicked.  Extracts the necessary
        information from the widget and raises a uniform signal for containing code
        """
        if not self._filter_model:
            return

        # get the currently selected index:
        selected_index = None
        selected_indexes = self._ui.entity_tree.selectionModel().selectedIndexes()
        if len(selected_indexes) != 1:
            return

        # extract the selected model index from the selection:
        selected_index = self._filter_model.mapToSource(selected_indexes[0])

        # determine the currently selected entity:
        entity_model = selected_index.model()
        entity_item = entity_model.itemFromIndex(selected_index)
        entity = entity_model.get_entity(entity_item)
        if not entity:
            return

        if entity["type"] == "Step":
            # can't create tasks on steps as we don't have an entity!
            return

        step = None
        if entity["type"] == "Task":
            step = entity.get("step")
            entity = entity.get("entity")
            if not entity:
                return

        # and emit the signal for this entity:
        self.create_new_task.emit(entity, step)

    def _build_breadcrumb_trail(self):
        """
        """
        breadcrumbs = []

        selected_indexes = self._ui.entity_tree.selectionModel().selectedIndexes()
        if len(selected_indexes) != 1:
            return {}

        src_model = self._filter_model.sourceModel()

        # now, walk up the tree
        idx = self._filter_model.mapToSource(selected_indexes[0])
        while idx.isValid():
            entity = src_model.get_entity(src_model.itemFromIndex(idx))
            if entity:
                name_token = "content" if entity["type"] == "Task" else "name"
                label = "<b>%s</b> %s" % (entity["type"], entity.get(name_token))
                breadcrumbs.append(EntityTreeForm._EntityBreadcrumb(label, entity))
            else:
                label = get_model_str(idx)
                breadcrumbs.append(Breadcrumb(label))

            idx = idx.parent()

        # return reversed list:
        return breadcrumbs[::-1]

    def _emit_entity_selected(self, entity_details):
        """
        """
        breadcrumbs = self._build_breadcrumb_trail()
        self.entity_selected.emit(entity_details, breadcrumbs)

